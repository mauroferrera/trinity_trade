"""Persistencia: la ÚNICA capa que habla con SQLite.

Todo lo que entra o sale de `trading.db` pasa por aquí. Por debajo, `database.models`
es la fuente de verdad del esquema y `core.clock` la del tiempo; este módulo solo
escribe SQL y traduce filas.

Tres cosas cambiaron respecto a `REF/store.py`, y las tres por lo mismo: REF
importaba `tclock` (que importa MetaTrader5) y `strategy` (Fase 6) para sellar una
hora y leer un YAML. Eso hacía que `database/` no se pudiera importar —ni probar—
sin la pila completa, y que la ruta de la base estuviera definida en dos sitios.

  1. El reloj viene de `core.clock`, que es puro. No se tira nada: la mitad de
     broker de `tclock` sigue donde estaba y se conectará cuando haya MT5.
  2. La configuración entra por un seam inyectable (`set_config_source`), con un
     default perezoso que importa `strategy` al primer uso. Importar este módulo
     ya no arrastra la Fase 6, y los tests pueden meter una config falsa.
  3. `DB_PATH` se IMPORTA de `core.paths` en vez de recomponerse aquí. REF lo
     recomponía (`os.path.join(PROJECT_DIR, "trading.db")`) mientras
     `core.paths` tenía su propia constante, y solo coincidían por casualidad;
     su propio `test_paths.py` ya exigía que fueran iguales. En este repo las
     rutas son `database/trading.db` y `config/strategy.yaml`, así que dos
     constantes habrían apuntado a ficheros distintos y la mitad de las escrituras
     se habrían ido al lugar equivocado en silencio.
"""

import json
import sqlite3
import uuid
import warnings
from contextlib import closing
from datetime import datetime, timedelta
from typing import Optional, Protocol, runtime_checkable

from core import clock
from core import paths as _paths
from database import models

# Delegadas en core.paths: la raíz se computa UNA vez (ver el docstring de ese
# módulo para por qué importa). Se reexportan aquí porque PROJECT_DIR y DB_PATH son
# parte de la API pública de este módulo — scripts/, research/ y tests los importan
# por su nombre, y mover la constante fuera los rompería sin avisar.
PROJECT_DIR = _paths.PROJECT_DIR

# Anclada al DIRECTORIO DEL PROYECTO, nunca al CWD del proceso. Con la ruta
# relativa "trading.db", lanzar la app o la suite desde otro directorio abría (o
# creaba de cero, con el esquema completo) un SEGUNDO trading.db: toda la
# auditoría parecía haberse evaporado mientras seguía entera, en otro fichero.
# Escribir contra la copia equivocada es la peor forma de perder un activo: no
# da error, da silencio.
DB_PATH = _paths.DB_PATH


def now_iso():
    """Sello de auditoría UTC (+00:00). Delegado en core.clock para que exista UN
    solo generador de sellos en todo el proyecto."""
    return clock.now_iso()


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _safe_json(raw, default):
    """Parsea una columna JSON sin que UNA fila corrupta tumbe el listado entero.

    `json.loads` LANZA sobre JSON invalido, no devuelve el default. En un listado
    eso no pierde solo la fila: la excepcion sube antes de devolver nada y se
    llevan por delante TODAS las demas. setup_log tiene cinco columnas JSON y la
    auditoria depende de las cinco.

    Esto es el espejo en Python del `json_valid` que ya protege las queries de
    enlace (link_last_setup, get_setup_for_ticket, settle_trade_outcome). El guard
    SQL evita que `json_extract` de SQLite lance; este evita que `json.loads` de
    Python lo haga. Sin la segunda mitad, una sola fila con JSON roto devolvia 500
    en /api/risk/audit en vez de "no hay evidencia", y se caia el post-mortem entero.
    """
    if raw is None or raw == "":
        return default
    try:
        out = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default
    # Un JSON valido pero de otra forma (una lista donde se espera un dict) rompe
    # igual a quien lo consume, asi que se normaliza aqui y no en cada llamada.
    if isinstance(default, (dict, list)) and not isinstance(out, type(default)):
        return default
    return out


# ============================================================
# Seam de configuración
# ============================================================
#
# Estas siete funciones de REF delegaban en `strategy`, un módulo de la Fase 6
# que además importa `risk_engine` y `yaml`. Con la delegación escrita en el
# cuerpo, importar `store` cargaba la Fase 6 entera: no se podía probar la capa
# de persistencia sin la de estrategia, y sin PILA completa.
#
# Con un seam, el default sigue siendo `strategy` —comportamiento idéntico en
# producción— pero es un import TARDÍO: la Fase 6 solo entra si alguien pide la
# config. Un test mete un `FakeConfigSource` y exercises todo el CRUD sin YAML.


class ConfigUnavailable(RuntimeError):
    """No hay `ConfigSource` utilizable (normalmente: aún no existe `strategy`).

    Existe como tipo propio, y no un `RuntimeError` genérico, porque hay dos
    reacciones MUY distintas ante el mismo fallo y hay que poder distinguirlas:
    `default_trading_config()` debe estallar (quien la llama la quiere de verdad),
    mientras que la siembra de un rol vacío debe poder seguir. Ver `_seed_defaults`.
    """


@runtime_checkable
class ConfigSource(Protocol):
    """Lo que `store` necesita de la configuración. Lo mínimo, no la API de `strategy`."""

    def get_config(self) -> dict: ...
    def save_config(self, cfg: dict) -> None: ...
    def data_sources(self) -> dict: ...
    def agent_topics(self) -> dict: ...
    def watcher_config(self) -> dict: ...


class _LazyStrategyConfig:
    """Default del seam: traduce a la API de `strategy` cuando hace falta.

    El import va dentro del método a propósito. Si estuviera arriba del todo,
    bastaría con que un test importara `store` para que `strategy` —y con él
    `yaml` y `risk_engine`— se cargara igual, y el seam no serviría de nada.
    """

    def _mod(self):
        try:
            import strategy  # noqa: PLC0415 - tardío a propósito (ver docstring)
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise ConfigUnavailable(
                "store necesita la configuración y 'strategy' todavía no existe "
                "(Fase 6). Inyecta una con store.set_config_source(...), o llama "
                "a las funciones de DB que no dependen de la config."
            ) from exc
        return strategy

    def get_config(self) -> dict:
        return self._mod().get_config()

    def save_config(self, cfg: dict) -> None:
        self._mod().save(cfg or {})

    def data_sources(self) -> dict:
        return self._mod().get_data_sources()

    def agent_topics(self) -> dict:
        return self._mod().get_agent_topics()

    def watcher_config(self) -> dict:
        return self._mod().get_watcher_config() or {}


_config_source: ConfigSource = _LazyStrategyConfig()


def set_config_source(source: ConfigSource | None) -> ConfigSource:
    """Sustituye el seam (tests, o un futuro lector de YAML nativo). Devuelve el anterior.

    `None` restaura el default perezoso.
    """
    global _config_source
    previous = _config_source
    _config_source = source if source is not None else _LazyStrategyConfig()
    return previous


def get_config_source() -> ConfigSource:
    return _config_source


# ============================================================
# Semilla del agente
# ============================================================
#
# REF calculaba esto a nivel de módulo (`_SEED_CFG = get_config()`), lo que
# obligaba a leer el YAML en cada `import store`, tests incluidos. Las tres
# constantes no las usa nadie fuera de este fichero (comprobado), así que pasan
# a funciones: se evalúan al sembrar, no al importar.

DEFAULT_SETTINGS = {
    "active_role_id": "general",
    "fallback_order": json.dumps(["gemini/gemini-3.8-flash", "gemini/gemini-3.5-flash"]),
    "history_limit": "20",
    "max_tool_rounds": "6",
}


def _seed_config() -> dict:
    return _config_source.get_config() or {}


def _seed_system_prompt(cfg: dict) -> str:
    """Prompt del rol 'general': system_prompt + risk_policy del YAML."""
    prompt = cfg.get("agent_system_prompt") or ""
    risk = cfg.get("agent_risk_policy") or ""
    data_sources = cfg.get("agent_data_sources_policy") or ""
    parts = [p for p in (prompt, data_sources, risk) if p]
    return "\n\n".join(parts)


def seed_roles() -> list[dict]:
    """Roles iniciales. El prompt se arma desde la config en el momento del sembrado."""
    cfg = _seed_config()
    return [
        {
            "id": "general",
            "name": "Asistente Trading",
            "system_prompt": _seed_system_prompt(cfg),
            "allowed_tools": cfg.get("agent_allowed_tools") or [],
            "provider": "auto",
            "model": "",
        },
    ]


def default_trading_config() -> dict:
    """La config de trading vive en `config/strategy.yaml` (fuente de verdad).

    Devuelve el shape histórico (risk_weights/killzones como JSON string), que es
    lo que esperan `get_config_summary()` y los EAs.
    """
    return _seed_config()


def init_db():
    """Crea el esquema si no existe, aplica migraciones y siembra lo inicial.

    El DDL vive en `database.models` (fuente de verdad, con `schema.sql` generado
    a partir de ahí). Aquí solo se orquesta: esquema, semilla y la migración de
    datos de horas legacy, que NO es de esquema y por eso vive aparte.
    """
    with closing(get_db()) as conn:
        models.init_schema(conn)
        _seed_defaults(conn)
        conn.commit()
    # Fuera de la transacción: es una migración de datos, no de esquema, y su
    # propio commit no debe mezclarse con el resto del init.
    _migrate_legacy_local_times()


def _seed_defaults(cursor_conn):
    """Filas iniciales del agente: rol general y ajustes por defecto.

    Separado de `init_db` porque necesita la config (para el prompt del rol), y
    la config es un seam perezoso: se lee aquí, cuando toca, no al importar.
    """
    cursor = cursor_conn.cursor()
    try:
        roles = seed_roles()
    except ConfigUnavailable:
        # El prompt del rol sale del YAML de strategy, y strategy es de la Fase 6.
        # Eso NO puede ser motivo para que `init_db()` falle: crear el esquema
        # es un requisito previo a tener estrategia, no al revés. Se siembra el
        # rol 'general' sin prompt y ya se rellenará cuando la config exista.
        roles = [
            {
                "id": "general",
                "name": "Asistente Trading",
                "system_prompt": "",
                "allowed_tools": [],
                "provider": "auto",
                "model": "",
            },
        ]
        warnings.warn(
            "init_db() sembró el rol 'general' con el prompt vacío porque no hay "
            "config disponible (¿Fase 6 sin ejecutar?). Se rellenará al reiniciar "
            "con la config presente.",
            RuntimeWarning,
            stacklevel=2,
        )
    cursor.execute("SELECT COUNT(*) AS n FROM roles")
    if cursor.fetchone()["n"] == 0:
        for r in roles:
            cursor.execute(
                "INSERT OR REPLACE INTO roles (id, name, system_prompt, allowed_tools, provider, model, active, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (r["id"], r["name"], r["system_prompt"], json.dumps(r["allowed_tools"]), r["provider"], r["model"], now_iso()),
            )
    else:
        # Roles legados se conservan pero inactivos; general pasa a ser el único activo
        general = roles[0]
        cursor.execute(
            "UPDATE roles SET name=?, system_prompt=?, allowed_tools=?, provider=?, model=?, active=1, updated_at=? WHERE id=?",
            (general["name"], general["system_prompt"], json.dumps(general["allowed_tools"]), general["provider"], general["model"], now_iso(), "general"),
        )
        cursor.execute("UPDATE roles SET active=0 WHERE id != ?", ("general",))
    for k, v in DEFAULT_SETTINGS.items():
        cursor.execute("INSERT OR IGNORE INTO agent_settings (key, value) VALUES (?, ?)", (k, v))
    for k, v in DEFAULT_SETTINGS.items():
        if k in ("fallback_order", "max_tool_rounds"):
            cursor.execute(
                "INSERT INTO agent_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, v),
            )

# ============================================================
# Settings
# ============================================================

def get_settings():
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT key, value FROM agent_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


def set_setting(key, value):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO agent_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


# ============================================================
# Configuración de trading (espejo de los EAs MQL5)
# ============================================================
#
# Estas cinco NO leen la base: son el espejo de `config/strategy.yaml`, que es la
# fuente de verdad (ver D-006). Viven aquí porque el API las consume por estos
# nombres y cambiarlas de sitio rompería las rutas; la config llega por el seam.

def get_trading_config():
    return _config_source.get_config()


def set_trading_config(cfg):
    _config_source.save_config(cfg or {})


def get_data_sources():
    return _config_source.data_sources()


def get_agent_topics():
    return _config_source.agent_topics()


def get_config_summary():
    """Resumen de reglas activas (visible en la UI) a partir del YAML."""
    cfg = _config_source.get_config() or {}
    risk_weights = cfg.get("risk_weights") or "{}"
    killzones = cfg.get("killzones") or "[]"
    try:
        weights = json.loads(risk_weights) if isinstance(risk_weights, str) else risk_weights
    except (json.JSONDecodeError, TypeError):
        weights = {}
    try:
        kz = json.loads(killzones) if isinstance(killzones, str) else killzones
    except (json.JSONDecodeError, TypeError):
        kz = []
    return {
        "risk_weights": weights,
        "killzones": [{"name": w.get("name"), "start": w.get("start"), "end": w.get("end")}
                      for w in kz if isinstance(w, dict)],
        "data_sources": cfg.get("data_sources") or {},
        "min_score": cfg.get("min_score"),
        "min_rr": cfg.get("min_rr"),
        "setup_ttl_minutes": cfg.get("setup_ttl_minutes"),
        "max_trades_day": cfg.get("max_trades_day"),
        "risk_pct": cfg.get("risk_pct"),
        "loss_limit_fixed": cfg.get("loss_limit_fixed"),
        "loss_limit_pct": cfg.get("loss_limit_pct"),
        "prop_enabled": bool(cfg.get("prop_enabled")),
        "prop_max_dd_daily_pct": cfg.get("prop_max_dd_daily_pct"),
        "prop_max_dd_total_pct": cfg.get("prop_max_dd_total_pct"),
        "prop_max_profit_day_pct": cfg.get("prop_max_profit_day_pct"),
        "prop_consistency_days": cfg.get("prop_consistency_days"),
        "news_buffer_min": cfg.get("news_buffer_min"),
        "agent_risk_policy": cfg.get("agent_risk_policy") or "",
    }


# ============================================================
# Setup log (post-mortem del risk engine, M3) — append-only
# ============================================================

def log_setup(entry: dict):
    """Registra de forma inmutable cada disparo del motor de riesgo.

    entry: symbol, timeframe, direction, verdict, score, breakdown(comps),
           entry/sl/target, invalidate_level, validated, reject_reasons,
           risk_state, trade_result, context (decision_context.build_context),
           source ('' = producción).
    Devuelve el id de la fila (para poder completarla después con
    update_trade_result) o None si el registro falla: la auditoría nunca debe
    tumbar la ejecución.
    """
    context = entry.get("context") or {}
    if not isinstance(context, dict):
        context = {}
    age = context.get("snapshot_age_s")
    with closing(get_db()) as conn:
        cur = conn.execute(
            """
            INSERT INTO setup_log (
                symbol, timeframe, direction, verdict, score, breakdown_json,
                entry, sl, target, invalidate_level, validated, reject_reasons,
                risk_state_json, trade_result_json, context_json, snapshot_age_s,
                source, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(entry.get("symbol") or "EURUSD").upper(),
                str(entry.get("timeframe") or "M15").upper(),
                str(entry.get("direction") or "BUY").upper(),
                str(entry.get("verdict") or ""),
                float(entry.get("score") or 0.0),
                json.dumps(entry.get("breakdown") or {}, ensure_ascii=False),
                entry.get("entry"),
                entry.get("sl"),
                entry.get("target"),
                entry.get("invalidate_level"),
                1 if entry.get("validated") else 0,
                json.dumps(entry.get("reject_reasons") or [], ensure_ascii=False),
                json.dumps(entry.get("risk_state") or {}, ensure_ascii=False),
                json.dumps(entry.get("trade_result") or {}, ensure_ascii=False),
                json.dumps(context, ensure_ascii=False),
                float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else None,
                str(entry.get("source") or ""),
                now_iso(),
            ),
        )
        conn.commit()
        return cur.lastrowid


def update_trade_result(setup_id, result: dict):
    """Completa trade_result_json de una fila ya registrada (merge, no replace).

    Lo usa send_market_order para dejar los intentos de llenado (retcode de cada
    order_send) junto a la decisión que los provoked. Se hace merge y no
    escritura ciega porque la misma fila puede recibir después el ticket que
    enlaza link_last_setup.
    """
    if not setup_id:
        return False
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT trade_result_json FROM setup_log WHERE id = ?", (int(setup_id),)
        ).fetchone()
        if row is None:
            return False
        try:
            current = json.loads(row["trade_result_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current.update(result or {})
        conn.execute(
            "UPDATE setup_log SET trade_result_json = ? WHERE id = ?",
            (json.dumps(current, ensure_ascii=False), int(setup_id)),
        )
        conn.commit()
        return True


def list_setup_log(limit: int = 100, verdict: str = "", symbol: str = ""):
    """Consulta del post-mortem para /api/risk/audit."""
    q = "SELECT * FROM setup_log WHERE 1=1"
    args: list = []
    if verdict:
        q += " AND verdict = ?"
        args.append(verdict)
    if symbol:
        q += " AND symbol = ?"
        args.append(str(symbol).upper())
    q += " ORDER BY timestamp DESC LIMIT ?"
    args.append(int(limit))
    with closing(get_db()) as conn:
        rows = conn.execute(q, args).fetchall()
        return [
            {
                "id": r["id"],
                "symbol": r["symbol"],
                "timeframe": r["timeframe"],
                "direction": r["direction"],
                "verdict": r["verdict"],
                "score": r["score"],
                "breakdown": _safe_json(r["breakdown_json"], {}),
                "entry": r["entry"],
                "sl": r["sl"],
                "target": r["target"],
                "invalidate_level": r["invalidate_level"],
                "validated": bool(r["validated"]),
                "reject_reasons": _safe_json(r["reject_reasons"], []),
                "risk_state": _safe_json(r["risk_state_json"], {}),
                "trade_result": _safe_json(r["trade_result_json"], {}),
                "context": _safe_json(r["context_json"], {}),
                "snapshot_age_s": r["snapshot_age_s"],
                "source": r["source"] or "",
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]


# El enlace de un fill a su setup es la base de TODO el post-mortem por
# componente: si se engancha al setup equivocado, cada win-rate, expectativa y
# calibración posterior son basura con apariencia de dato. Por eso el destino se
# decide con una regla escrita y el desempate se DECLARA, nunca se resuelve
# eligiendo en silencio.
LINK_TOLERANCE_RATIO = 0.5   # |entry - price| <= ratio * |entry - sl|
LINK_WINDOW_SEC = 900.0      # y dentro de esta ventana respecto al fill


def pick_link_target(candidates, price_open=None,
                     tolerance_ratio: float = LINK_TOLERANCE_RATIO) -> dict:
    """Elige (o se niega a elegir) la fila de setup_log de un fill. PURO.

    candidates: [{id, entry, sl}] — cualquier iterable de filas ya filtradas por
    símbolo/ventana por el llamante. price_open: precio real del fill.

    Regla de admisión: |entry - price_open| <= tolerance_ratio * |entry - sl|, o
    sea, la fila tiene que estar a menos de media distancia al stop del precio que
    se llenó de verdad. Dos filas a más de medio SL una de otra son dos
    operaciones distintas, no dos lecturas del mismo fill.

    Entre las admitidas gana la más cercana. Si el empate es exacto, NO gana
    nadie: el lote se marca entero como ambiguo. Una auditoría que falla se
    detecta y se repite; una atribución silenciosa equivocada no la detecta
    nadie, porque ya no queda rastro de que hubiera una elección.

    Sin `price_open` no hay forma de desambiguar por precio: solo se enlaza si
    hay exactamente un candidato.
    """
    rows = [r for r in (candidates or []) if r.get("id") is not None]
    out = {"winner": None, "ambiguous": [], "distances": {}, "reason": ""}
    if not rows:
        out["reason"] = "sin setup_log candidato"
        return out

    def _dist(row):
        try:
            return abs(float(row["entry"]) - float(price_open))
        except (TypeError, ValueError, KeyError):
            return None

    if price_open is None:
        with_entry = [r for r in rows if r.get("entry") is not None]
        if len(with_entry) != 1:
            out["reason"] = ("sin precio de fill: %d candidatos, no se elige"
                             % len(with_entry))
            out["ambiguous"] = [r["id"] for r in with_entry]
            return out
        out["winner"] = with_entry[0]["id"]
        out["reason"] = "único candidato"
        return out

    admitted = []
    for r in rows:
        d = _dist(r)
        if d is None:
            continue
        try:
            sl_dist = abs(float(r["entry"]) - float(r["sl"]))
        except (TypeError, ValueError, KeyError):
            sl_dist = 0.0
        out["distances"][r["id"]] = round(d, 10)
        if sl_dist > 0 and d <= tolerance_ratio * sl_dist:
            admitted.append((d, r["id"]))
    if not admitted:
        out["reason"] = "ningún setup_log a menos de %.0f%% de |entry-sl| del fill" % (
            tolerance_ratio * 100.0)
        return out
    if len(admitted) == 1:
        out["winner"] = admitted[0][1]
        out["reason"] = "único candidato dentro de tolerancia"
        return out

    admitted.sort(key=lambda t: t[0])
    best_d, best_id = admitted[0]
    runner_d, runner_id = admitted[1]
    if runner_d - best_d <= 1e-12:
        out["ambiguous"] = [i for _, i in admitted]
        out["reason"] = "empate exacto a %.10f de distancia" % best_d
        return out
    out["winner"] = best_id
    out["ambiguous"] = [i for _, i in admitted[1:]]
    out["reason"] = "más cercana (%.10f vs %.10f)" % (best_d, runner_d)
    return out


def link_last_setup(ticket, symbol="EURUSD", price_open=None, sl=None, tp=None,
                    window_sec: float = LINK_WINDOW_SEC, now=None) -> dict:
    """Vincula el ticket de la orden ejecutada a SU setup_log, para poder medir
    win-rate por componente después.

    Antes: se cogía la fila aprobada más reciente del símbolo con
    `trade_result_json = '{}' ORDER BY timestamp DESC LIMIT 1`. Eso no es un
    enlace, es una suposición: si el watcher aprobó dos setups del mismo símbolo
    en el mismo cuarto de hora, la orden ejecutada acababa atribuida al que
    fuera, y el audit mostraba un win-rate real de una operación que no fue esa.

    Ahora el destino lo decide `pick_link_target` con la regla de distancia y
    ventana de arriba, y las filas que pierden o empatan quedan marcadas con
    {"link": "ambiguous"} en vez de quedarse reclamando el ticket en silencio.

    El filtro de candidatas excluye por `$.link`, NO por `$.ticket`: desde que
    send_market_order graba los intentos de llenado (0.7) la fila recién
    ejecutada ya trae su ticket en trade_result_json, así que excluir por ticket
    dejaba a la única fila buena fuera de la lista y el enlace no ocurría nunca.
    "Sin ticket asignado" significa "sin enlace asignado".

    Devuelve {"linked", "setup_id", "ambiguous", "candidates", "reason"}.
    """
    sym = str(symbol or "EURUSD").upper()
    ts_now = clock.to_utc(now) if isinstance(now, datetime) else clock.now_utc()
    since = clock.now_iso(ts_now - timedelta(seconds=float(window_sec)))
    until = clock.now_iso(ts_now)

    def _payload_of(conn, sid):
        row = conn.execute(
            "SELECT trade_result_json FROM setup_log WHERE id = ?", (sid,)
        ).fetchone()
        try:
            payload = json.loads((row or {"trade_result_json": "{}"})["trade_result_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT id, entry, sl FROM setup_log WHERE symbol = ? AND validated = 1 "
            # json_extract devuelve NULL para las claves ausentes, así que '{}'
            # (la fila recién decisionada) sigue entrando. `json_valid` va antes
            # porque json_extract LANZA sobre JSON inválido en vez de devolver
            # NULL: sin el, una sola fila corrupta haría fallar el enlace de
            # todos los trades, no solo del de esa fila.
            "AND json_valid(trade_result_json) "
            "AND json_extract(trade_result_json, '$.link') IS NULL "
            "AND json_extract(trade_result_json, '$.dry_run') IS NULL "
            "AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC",
            (sym, since, until),
        ).fetchall()
        cands = [dict(r) for r in rows]
        if not cands:
            return {"linked": False, "setup_id": None, "ambiguous": [],
                    "candidates": 0, "reason": "sin setup_log candidato"}

        pick = pick_link_target(cands, price_open=price_open)
        winner = pick["winner"]
        ambiguous = [i for i in pick["ambiguous"] if i != winner]

        for sid in ambiguous:
            payload = _payload_of(conn, sid)
            payload.update({"link": "ambiguous", "ticket": ticket,
                            "link_distances": pick["distances"]})
            conn.execute(
                "UPDATE setup_log SET trade_result_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), sid),
            )

        if winner is None:
            conn.commit()
            return {"linked": False, "setup_id": None, "ambiguous": ambiguous,
                    "candidates": len(cands), "reason": pick["reason"]}

        # Merge, no replace: la fila puede venir ya con los intentos de llenado
        # de send_market_order (attempts/filled/no_fill) y escribirlos encima
        # dejaría la tasa de no-fill sin forma de medirse.
        payload = _payload_of(conn, winner)
        payload.update({"ticket": ticket, "price_open": price_open, "sl": sl, "tp": tp,
                        "link": "linked", "link_rule": pick["reason"]})
        if ambiguous:
            payload["beat_ambiguous"] = ambiguous
        conn.execute(
            "UPDATE setup_log SET trade_result_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), winner),
        )
        conn.commit()
        return {"linked": True, "setup_id": winner, "ambiguous": ambiguous,
                "candidates": len(cands), "reason": pick["reason"]}


# ---------------------------------------------------------------------------
# Cierre: lo que un trade cerrado deja en la fila que lo decidió
# ---------------------------------------------------------------------------

def record_excursions(positions) -> int:
    """Acumula el extremo a favor / en contra de cada posición ABIERTA. Puro-ish.

    MT5 no expone MFE/MAE en la API de deals: no existe el campo. Lo único que
    hay es mirar el precio mientras la posición vive, y quedarse con el extremo.
    Eso SUBESTIMA el máximo real —entre dos muestras el precio puede haber ido
    mucho más lejos y vuelto— así que se guarda el contador de muestras y quien
    lo lee tiene que saber con qué frecuencia se observó.

    `best` es el extremo MÁS A FAVOR y `worst` el MÁS EN CONTRA, ya resueltos
    según la dirección: en un SELL el precio bajo es a favor. Guardar un high/low
    crudo obligaría a quien lo leyera a acertar el signo cada vez, que es
    exactamente el error que después se atribuye al score.

    Idempotente en el sentido que importa: repetir el mismo precio no cambia los
    extremos (solo suma una muestra). Devuelve cuántas posiciones se tocaron.
    """
    written = 0
    with closing(get_db()) as conn:
        for p in (positions or []):
            try:
                ticket = int(p["ticket"])
                price = float(p.get("price_current"))
            except (KeyError, TypeError, ValueError):
                continue
            direction = str(p.get("type") or p.get("direction") or "").upper()
            if direction not in ("BUY", "SELL") or price <= 0:
                continue
            row = conn.execute(
                "SELECT best_price, worst_price, samples, first_seen "
                "FROM trade_excursions WHERE ticket = ?", (ticket,),
            ).fetchone()
            now = clock.now_iso()
            if row is None:
                best = worst = price
                samples = 1
                first_seen = now
            else:
                fav, adv = (max, min) if direction == "BUY" else (min, max)
                best = fav(price, row["best_price"] if row["best_price"] is not None else price)
                worst = adv(price, row["worst_price"] if row["worst_price"] is not None else price)
                samples = int(row["samples"] or 0) + 1
                first_seen = row["first_seen"] or now
            conn.execute(
                "INSERT OR REPLACE INTO trade_excursions "
                "(ticket, symbol, direction, price_open, best_price, worst_price, "
                " samples, first_seen, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket, str(p.get("symbol") or ""), direction, p.get("price_open"),
                 best, worst, samples, first_seen, now),
            )
            written += 1
        conn.commit()
    return written


def get_excursion(ticket) -> Optional[dict]:
    """Excursión muestreada de un ticket, o None si nunca se observó."""
    try:
        ticket = int(ticket)
    except (TypeError, ValueError):
        return None
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM trade_excursions WHERE ticket = ?", (ticket,)
        ).fetchone()
    return dict(row) if row else None


def find_setup_by_ticket(ticket) -> Optional[int]:
    """setup_log.id cuya trade_result_json reclama este ticket.

    Se busca por `$.ticket` y no por `$.link = 'linked'` a propósito: la fila
    puede llevar el ticket puesto por `link_last_setup` o haberlo adquirido antes
    por otro camino, y el que importa para el post-mortem es el que la decisión
    puede reclamar como suyo.
    """
    try:
        ticket = int(ticket)
    except (TypeError, ValueError):
        return None
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT id FROM setup_log "
            # json_valid antes que json_extract: ver get_setup_for_ticket.
            "WHERE json_valid(trade_result_json) "
            "AND json_extract(trade_result_json, '$.ticket') = ? "
            "ORDER BY id DESC LIMIT 1", (ticket,),
        ).fetchone()
    return int(row["id"]) if row else None


def get_setup_for_ticket(ticket) -> Optional[dict]:
    """La fila de setup_log que reclama este ticket, o None.

    Trae la `trade_result_json` YA PARSEADA porque el cierre necesita el SL que
    `link_last_setup` dejó ahí: sin el SL no hay contra qué dividir para sacar R,
    y adivinarlo desde `sl_distance` de la config sería medir el riesgo de un
    stop que el broker nunca vio.
    """
    try:
        ticket = int(ticket)
    except (TypeError, ValueError):
        return None
    with closing(get_db()) as conn:
        row = conn.execute(
            # `json_valid` NO es opcional: `json_extract` de SQLite LANZA
            # OperationalError("malformed JSON") sobre una fila corrupta en vez
            # de devolver NULL, así que una sola fila con trade_result_json
            # inválido tumba la consulta ENTERA. En el post-mortem eso es
            #bri Fatal: se pierde el enlace de todos los trades, no del malo.
            "SELECT id, symbol, direction, score, verdict, breakdown_json, trade_result_json, "
            "timestamp FROM setup_log "
            "WHERE json_valid(trade_result_json) "
            "AND json_extract(trade_result_json, '$.ticket') = ? "
            "ORDER BY id DESC LIMIT 1", (ticket,),
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    for key, src in (("breakdown", "breakdown_json"), ("trade_result", "trade_result_json")):
        try:
            parsed = json.loads(out.get(src) or "{}")
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        out[key] = parsed if isinstance(parsed, dict) else {}
    return out


def attach_outcome_to_setup(ticket, outcome: dict) -> Optional[int]:
    """Mergea el resultado del trade cerrado en la fila de setup que lo enlazó.

    Merge, nunca replace: la fila ya trae la decisión (intentos de llenado,
    enlace, precio) y pisarla perdería el motivo de por qué se operó.

    Idempotente: llamarlo dos veces con el mismo cierre deja lo mismo, y por eso
    se puede barrer en cada sync sin miedo a duplicar nada.
    """
    setup_id = find_setup_by_ticket(ticket)
    if setup_id is None:
        return None
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT trade_result_json FROM setup_log WHERE id = ?", (setup_id,)
        ).fetchone()
        try:
            payload = json.loads((row or {"trade_result_json": "{}"})["trade_result_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        # Un cierre NUNCA pisa un R ya calculado con más información: si el
        # barrido va con menos datos que los que ya tenía la fila, se conserva
        # lo que había. Sin esto, un sync sin excursion dejaría mfe_r a null
        # encima de un mfe_r bueno.
        for key, value in (outcome or {}).items():
            if value is None and payload.get(key) is not None:
                continue
            payload[key] = value
        payload["outcome_recorded_at"] = clock.now_iso()
        conn.execute(
            "UPDATE setup_log SET trade_result_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), setup_id),
        )
        conn.commit()
    return setup_id


# ============================================================
# Watcher (bot a la escucha) — config persistente + estado por símbolo
# ============================================================

def _watcher_row(r):
    return {
        "symbol": r["symbol"],
        "timeframe": r["timeframe"],
        "direction": r["direction"],
        "score": r["score"],
        "verdict": r["verdict"],
        "entry": r["entry"],
        "sl": r["sl"],
        "target": r["target"],
        "invalidate_level": r["invalidate_level"],
        "status": r["status"],
        "notified": bool(r["notified"]),
        "auto_executed": bool(r["auto_executed"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def get_watcher_config():
    """Config del watcher desde strategy.yaml (DEFAULT_TRADING_CONFIG aplanado)."""
    return _config_source.watcher_config() or {}


def get_watcher_auto_execute():
    """auto_execute efectivo: el override persistido (UI) si existe; si no, el YAML."""
    val = None
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT value FROM watcher_settings WHERE key = 'auto_execute'"
        ).fetchone()
        val = row["value"] if row else None
    if val is not None:
        return str(val).lower() in ("1", "true", "yes")
    return bool((get_watcher_config() or {}).get("auto_execute", False))


def set_watcher_auto_execute(enabled):
    """Persiste el toggle auto_execute (overrides el valor del YAML)."""
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO watcher_settings (key, value) VALUES ('auto_execute', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if enabled else "0",),
        )
        conn.commit()


PROP_STATE_KEYS = ("baseline_equity", "baseline_day", "day_peak", "day_peak_day", "total_peak")


def get_prop_state():
    """Estado del gate prop-firm. Devuelve dict con los campos vigentes o None
    para los no persistidos aún (baseline sin crear, pico sin resetear)."""
    out = {k: None for k in PROP_STATE_KEYS}
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT key, value FROM prop_state WHERE key IN (%s)"
            % ",".join("?" * len(PROP_STATE_KEYS)),
            PROP_STATE_KEYS,
        ).fetchall()
    for r in rows:
        try:
            out[r["key"]] = float(r["value"])
        except (TypeError, ValueError):
            out[r["key"]] = r["value"]
    return out


def set_prop_state(**fields):
    """Persiste los campos prop_state dados (solo claves conocidas)."""
    payload = {k: v for k, v in fields.items() if k in PROP_STATE_KEYS}
    if not payload:
        return
    with closing(get_db()) as conn:
        for key, value in payload.items():
            conn.execute(
                "INSERT INTO prop_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
        conn.commit()


def get_setup_state(symbol, timeframe="M15"):
    """Estado vigente del watcher para un símbolo/timeframe (o None)."""
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM setup_state WHERE symbol = ? AND timeframe = ?",
            (str(symbol).upper(), str(timeframe).upper()),
        ).fetchone()
        return _watcher_row(row) if row else None


def list_setup_states():
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM setup_state ORDER BY symbol, timeframe").fetchall()
        return [_watcher_row(r) for r in rows]


def new_setup_state(symbol, timeframe, gate=None, status="active", notified=True):
    """Crea (o reemplaza) el estado activo de un setup detectado. created_at se
    reinicia: es un nuevo ciclo de alerta."""
    g = gate or {}
    ts = now_iso()
    with closing(get_db()) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO setup_state (
                symbol, timeframe, direction, score, verdict, entry, sl, target,
                invalidate_level, status, notified, auto_executed, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                str(symbol).upper(), str(timeframe).upper(),
                g.get("direction"), g.get("score"), g.get("verdict"),
                g.get("entry"), g.get("sl"), g.get("tp"),
                g.get("invalidate_level"), status,
                1 if notified else 0, ts, ts,
            ),
        )
        conn.commit()


def update_setup_state(symbol, timeframe, **fields):
    """Actualiza campos permitidos del estado (status/notified/auto_executed)."""
    allowed = ("status", "notified", "auto_executed")
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    ts = now_iso()
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT 1 FROM setup_state WHERE symbol = ? AND timeframe = ?",
            (str(symbol).upper(), str(timeframe).upper()),
        ).fetchone()
        if row is None:
            # Estado mínimo para poder registrar la transición sin un gate nuevo.
            conn.execute(
                """
                INSERT INTO setup_state (
                    symbol, timeframe, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(symbol).upper(), str(timeframe).upper(),
                 sets.get("status", "active"), now_iso(), ts),
            )
        cols = ", ".join(f"{k} = ?" for k in sets)
        conn.execute(
            f"UPDATE setup_state SET {cols}, updated_at = ? "
            "WHERE symbol = ? AND timeframe = ?",
            (*sets.values(), ts, str(symbol).upper(), str(timeframe).upper()),
        )
        conn.commit()


# ============================================================
# Chart drawings (herramientas manuales del gráfico)
# ============================================================

def get_drawings(symbol, timeframe="M15"):
    """Lista de dibujos persistidos para un symbol/timeframe (default [])."""
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT drawings_json FROM chart_drawings WHERE symbol = ? AND timeframe = ?",
            (str(symbol).upper(), str(timeframe).upper()),
        ).fetchone()
    if row is None:
        return []
    try:
        val = json.loads(row["drawings_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return val if isinstance(val, list) else []


def save_drawings(symbol, timeframe="M15", drawings=None):
    """Reemplaza (INSERT OR REPLACE) el set completo de dibujos del symbol/timeframe."""
    items = drawings if isinstance(drawings, list) else []
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chart_drawings (symbol, timeframe, drawings_json, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (str(symbol).upper(), str(timeframe).upper(),
             json.dumps(items, ensure_ascii=False), now_iso()),
        )
        conn.commit()
    return items


def delete_drawings(symbol, timeframe="M15"):
    """Borra todos los dibujos del symbol/timeframe."""
    with closing(get_db()) as conn:
        conn.execute(
            "DELETE FROM chart_drawings WHERE symbol = ? AND timeframe = ?",
            (str(symbol).upper(), str(timeframe).upper()),
        )
        conn.commit()


# ============================================================
# Roles
# ============================================================

def _role_row(r):
    return {
        "id": r["id"],
        "name": r["name"],
        "system_prompt": r["system_prompt"],
        "allowed_tools": _safe_json(r["allowed_tools"], []),
        "provider": r["provider"],
        "model": r["model"],
    }


def list_roles(active_only=True):
    with closing(get_db()) as conn:
        if active_only:
            return [_role_row(r) for r in conn.execute("SELECT * FROM roles WHERE active=1 ORDER BY name").fetchall()]
        return [_role_row(r) for r in conn.execute("SELECT * FROM roles ORDER BY name").fetchall()]


def get_role(role_id):
    with closing(get_db()) as conn:
        r = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
        return _role_row(r) if r else None


def upsert_role(role_id, name, system_prompt, allowed_tools, provider="auto", model=""):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO roles (id, name, system_prompt, allowed_tools, provider, model, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, system_prompt=excluded.system_prompt, "
            "allowed_tools=excluded.allowed_tools, provider=excluded.provider, model=excluded.model, "
            "updated_at=excluded.updated_at",
            (role_id, name, system_prompt, json.dumps(allowed_tools), provider, model, now_iso()),
        )
        conn.commit()
    return get_role(role_id)


def delete_role(role_id):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))
        conn.commit()


# ============================================================
# Conversations / Messages
# ============================================================

def create_conversation(title="Nueva conversación", role_id="general"):
    cid = uuid.uuid4().hex
    ts = now_iso()
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, role_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (cid, title, role_id, ts, ts),
        )
        conn.commit()
    return {"id": cid, "title": title, "role_id": role_id, "created_at": ts, "updated_at": ts}


def list_conversations():
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS nmsg "
            "FROM conversations c ORDER BY c.updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_conversation(cid):
    with closing(get_db()) as conn:
        r = conn.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
        return dict(r) if r else None


def touch_conversation(cid):
    with closing(get_db()) as conn:
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), cid))
        conn.commit()


def delete_conversation(cid):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        conn.commit()


def add_message(conversation_id, role, content, tool_call_id=None, name=None):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_call_id, name, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, tool_call_id, name, now_iso()),
        )
        conn.commit()
    touch_conversation(conversation_id)


def get_messages(conversation_id, limit=20):
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT role, content, tool_call_id, name FROM messages "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        out = []
        for r in reversed(rows):
            # Las filas role='tool' se persisten solo como traza (debug); sin el
            # assistant tool_calls pareado serían huérfanas y romperían la API.
            if r["role"] == "tool":
                continue
            out.append({"role": r["role"], "content": r["content"]})
        return out


# ============================================================
# Chart Alerts (niveles visuales colocados por el agente)
# ============================================================

def _alert_row(r):
    return {
        "id": r["id"],
        "symbol": r["symbol"],
        "price": r["price"],
        "label": r["label"],
        "side": r["side"],
        "status": r["status"],
        "created_at": r["created_at"],
        "triggered_at": r["triggered_at"],
        "conditions": _safe_json(r["conditions"], []),
        "timeframe": r["timeframe"],
        "expires_at": r["expires_at"],
        "last_check": r["last_check"],
    }


def add_chart_alert(symbol, price, label=None, side=None, conditions=None, timeframe=None, expires_at=None):
    with closing(get_db()) as conn:
        cur = conn.execute(
            "INSERT INTO chart_alerts (symbol, price, label, side, status, conditions, timeframe, "
            "expires_at, created_at, triggered_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL)",
            (
                symbol.upper(),
                float(price),
                label,
                side,
                json.dumps(conditions or []),
                timeframe,
                expires_at,
                now_iso(),
            ),
        )
        conn.commit()
        aid = cur.lastrowid
    return get_chart_alert(aid)


def get_chart_alert(aid):
    with closing(get_db()) as conn:
        r = conn.execute("SELECT * FROM chart_alerts WHERE id = ?", (aid,)).fetchone()
        return _alert_row(r) if r else None


def list_chart_alerts(active_only=False, limit=100):
    q = "SELECT * FROM chart_alerts WHERE 1=1"
    if active_only:
        q += " AND status = 'active'"
    q += " ORDER BY id DESC LIMIT ?"
    with closing(get_db()) as conn:
        return [_alert_row(r) for r in conn.execute(q, (limit,)).fetchall()]


def touch_chart_alert(aid):
    with closing(get_db()) as conn:
        conn.execute("UPDATE chart_alerts SET last_check = ? WHERE id = ?", (now_iso(), aid))
        conn.commit()


def set_chart_alert_status(aid, status):
    ts = now_iso() if status == "triggered" else None
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE chart_alerts SET status = ?, triggered_at = COALESCE(?, triggered_at) WHERE id = ?",
            (status, ts, aid),
        )
        conn.commit()
    return get_chart_alert(aid)


# ============================================================
# COT (Commitments of Traders, CFTC)
# ============================================================

def upsert_cot_report(report_date, am_net, lf_net, nc_net,
                      cot_index=None, macro_bias=None, delta_am=None, delta_lf=None):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO cot_reports (report_date, am_net, lf_net, nc_net, cot_index_26w, "
            "macro_bias, delta_am, delta_lf, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(report_date) DO UPDATE SET "
            "am_net=excluded.am_net, lf_net=excluded.lf_net, nc_net=excluded.nc_net, "
            "cot_index_26w=COALESCE(excluded.cot_index_26w, cot_reports.cot_index_26w), "
            "macro_bias=COALESCE(excluded.macro_bias, cot_reports.macro_bias)",
            (report_date, am_net, lf_net, nc_net, cot_index, macro_bias, delta_am, delta_lf, now_iso()),
        )
        conn.commit()


def _cot_row(r):
    return {
        "report_date": r["report_date"],
        "am_net": r["am_net"],
        "lf_net": r["lf_net"],
        "nc_net": r["nc_net"],
        "cot_index_26w": r["cot_index_26w"],
        "macro_bias": r["macro_bias"],
        "delta_am": r["delta_am"],
        "delta_lf": r["delta_lf"],
    }


def list_cot_reports(limit=60):
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM cot_reports ORDER BY report_date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_cot_row(r) for r in rows]


def latest_cot_report():
    with closing(get_db()) as conn:
        r = conn.execute(
            "SELECT * FROM cot_reports ORDER BY report_date DESC LIMIT 1"
        ).fetchone()
    return _cot_row(r) if r else None


# ============================================================
# Trades (snapshot del historial MT5)
# ============================================================

# Formato de las columnas de tiempo. Es FIJO a propósito: los filtros por
# "últimos N días" comparan tiempos, y para que el resultado no dependa del
# frame de cada fila todos los valores llevan el mismo sufijo de offset y la
# misma longitud. Por eso el tiempo se guarda con offset explícito (+00:00) y
# no como "%Y-%m-%d %H:%M:%S" pelado.
UTC_FMT = "%Y-%m-%d %H:%M:%S+00:00"


def utc_stamp(at=None):
    """Instante -> string UTC con offset, listo para columnas y para comparar."""
    return clock.to_utc(at or clock.now_utc()).strftime(UTC_FMT)


def utc_filter(col, days):
    """Devuelve (fragmento SQL, params) para `col` en los últimos N días.

    Compara con `datetime(col)` y no con el string crudo a propósito. SQLite
    normaliza los dos formatos a un instante canónico, así que el filtro sigue
    siendo correcto si queda alguna fila legacy sin offset; la comparación
    lexicográfica directa NO lo era, porque "…09:00:00" (naive) y
    "…09:00:00+00:00" (UTC) son instantes distintos y se ordenaban por su
    forma, no por su tiempo. Con la local eso truncaba la ventana 3 h de más.
    """
    cutoff = utc_stamp(clock.now_utc() - timedelta(days=days))
    return f" AND datetime({col}) >= datetime(?)", [cutoff]


def _migrate_legacy_local_times():
    """Convierte a UTC los `time_close` que se guardaron en hora LOCAL.

    `trades.time_close` venía de `datetime.fromtimestamp(t)` (naive, hora del
    SO) mientras `synced_at` de la MISMA fila venía de `now_iso()` (UTC). La
    tabla se reconstruye en cada sync, pero `sync_trades` solo hace clear cuando
    hay filas, así que un historial vacío dejaba filas viejas mezcladas. Esta
    migración corre una vez en init_db y solo toca valores sin offset.
    """
    with closing(get_db()) as conn:
        cur = conn.execute(
            "SELECT ticket, time_close FROM trades WHERE time_close IS NOT NULL"
        )
        updates = []
        for row in cur.fetchall():
            value = row["time_close"] or ""
            if not value or value.endswith("+00:00") or value.endswith("Z"):
                continue
            try:
                naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            # El valor viejo era hora local del SO: su offset es justamente lo
            # que hay que restar para volver a UTC.
            local_off = naive.astimezone().utcoffset() or timedelta(0)
            updates.append(((naive - local_off).strftime(UTC_FMT), row["ticket"]))
        if updates:
            conn.executemany(
                "UPDATE trades SET time_close = ? WHERE ticket = ?", updates
            )
            conn.commit()
            return len(updates)
    return 0


def clear_trades():
    """Vacía la tabla espejo de operaciones (se re-llena en cada sync)."""
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()


def upsert_trades(trades):
    with closing(get_db()) as conn:
        for t in trades:
            conn.execute(
                "INSERT INTO trades (ticket, symbol, action, volume, price_open, price_close, "
                "profit, time_close, time_open, commission, swap, exit_reason, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticket) DO UPDATE SET profit=excluded.profit, "
                "price_close=excluded.price_close, time_close=excluded.time_close, "
                "time_open=excluded.time_open, commission=excluded.commission, "
                "swap=excluded.swap, exit_reason=excluded.exit_reason, "
                "synced_at=excluded.synced_at",
                (t.get("ticket"), t.get("symbol", "EURUSD"), t.get("action", ""), t.get("volume"),
                 t.get("price_open"), t.get("price_close"), t.get("profit"), t.get("time_close"),
                 t.get("time_open"), t.get("commission"), t.get("swap"), t.get("exit_reason"),
                 now_iso()),
            )
        conn.commit()


def list_trades(symbol=None, action=None, days=None, limit=200):
    q = "SELECT * FROM trades WHERE 1=1"
    params = []
    if symbol:
        q += " AND symbol = ?"
        params.append(symbol.upper())
    if action:
        q += " AND action = ?"
        params.append(action.upper())
    if days:
        # `time_close` se guarda en UTC con offset (UTC_FMT) y el corte también,
        # y la comparación va por datetime() para que el resultado no dependa de
        # si la fila tiene offset. Ver utc_filter.
        clause, args = utc_filter("time_close", days)
        q += clause
        params.extend(args)
    q += " ORDER BY time_close DESC LIMIT ?"
    params.append(limit)
    with closing(get_db()) as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


# ============================================================
# Journal / Bitácora (SMC)
# ============================================================

def _journal_row(r):
    return {
        "id": r["id"],
        "conversation_id": r["conversation_id"],
        "ticket": r["ticket"],
        "symbol": r["symbol"],
        "action": r["action"],
        "poi_type": r["poi_type"],
        "liquidity_swept": r["liquidity_swept"],
        "cme_confirmation": r["cme_confirmation"],
        "setup_json": _safe_json(r["setup_json"], {}),
        "emotion": r["emotion"],
        "plan_compliance": r["plan_compliance"],
        "tags": _safe_json(r["tags"], []),
        "notes": r["notes"],
        "timestamp": r["timestamp"],
        "entry_price": r["entry_price"],
        "sl_price": r["sl_price"],
        "tp_price": r["tp_price"],
        "time_open": r["time_open"],
        "time_close": r["time_close"],
    }


def add_journal_entry(entry):
    with closing(get_db()) as conn:
        cur = conn.execute(
            "INSERT INTO journal (conversation_id, ticket, symbol, action, poi_type, liquidity_swept, "
            "cme_confirmation, setup_json, emotion, plan_compliance, tags, notes, timestamp, "
            "entry_price, sl_price, tp_price, time_open, time_close) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.get("conversation_id"),
                entry.get("ticket"),
                entry.get("symbol", "EURUSD"),
                entry.get("action"),
                entry.get("poi_type"),
                entry.get("liquidity_swept"),
                entry.get("cme_confirmation"),
                json.dumps(entry.get("setup_json") or {}),
                entry.get("emotion"),
                entry.get("plan_compliance"),
                json.dumps(entry.get("tags") or []),
                entry.get("notes"),
                now_iso(),
                entry.get("entry_price"),
                entry.get("sl_price"),
                entry.get("tp_price"),
                entry.get("time_open"),
                entry.get("time_close"),
            ),
        )
        conn.commit()
        eid = cur.lastrowid
    return get_journal_entry(eid)


def get_journal_entry(eid):
    with closing(get_db()) as conn:
        r = conn.execute("SELECT * FROM journal WHERE id = ?", (eid,)).fetchone()
        return _journal_row(r) if r else None


def list_journal(symbol=None, days=None, limit=50):
    q = "SELECT * FROM journal WHERE 1=1"
    params = []
    if symbol:
        q += " AND symbol = ?"
        params.append(symbol.upper())
    if days:
        # Mismo contrato que list_trades: journal.timestamp lo escribe now_iso()
        # (UTC con offset), así que el corte se genera en UTC con offset. Antes
        # el corte era `datetime.now()` naive local, que en comparación de
        # strings queda 3 h por debajo del valor almacenado y por tanto recorta
        # 3 h de más de la ventana.
        clause, args = utc_filter("timestamp", days)
        q += clause
        params.extend(args)
    q += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with closing(get_db()) as conn:
        return [_journal_row(r) for r in conn.execute(q, params).fetchall()]


def update_journal_entry(eid, entry):
    """REEMPLAZO COMPLETO de la fila (semántica de PUT, no de PATCH).

    Todas las columnas se reescriben desde `entry`, así que cualquier clave que
    no venga en el dict se queda en NULL. Es deliberado: el único llamador es
    `PUT /api/journal/{jid}`, que manda el `JournalEntry` entero del modelo.

    Se documenta porque la firma NO lo delata: `get_journal_entry(eid)` devuelve
    la fila completa, así que pasarle ese mismo dict al revés funciona, y pasar
    un dict parcial parece la opción obvious hasta que se descubre que acaba de
    borrar las notas. Si algún día hace falta actualización parcial, es otro
    método (o `COALESCE` con las claves presentes), no un cambio de esta función.
    """
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE journal SET conversation_id=?, ticket=?, symbol=?, action=?, poi_type=?, liquidity_swept=?, "
            "cme_confirmation=?, setup_json=?, emotion=?, plan_compliance=?, tags=?, notes=?, "
            "entry_price=?, sl_price=?, tp_price=?, time_open=?, time_close=? WHERE id=?",
            (
                entry.get("conversation_id"),
                entry.get("ticket"),
                entry.get("symbol", "EURUSD"),
                entry.get("action"),
                entry.get("poi_type"),
                entry.get("liquidity_swept"),
                entry.get("cme_confirmation"),
                json.dumps(entry.get("setup_json") or {}),
                entry.get("emotion"),
                entry.get("plan_compliance"),
                json.dumps(entry.get("tags") or []),
                entry.get("notes"),
                entry.get("entry_price"),
                entry.get("sl_price"),
                entry.get("tp_price"),
                entry.get("time_open"),
                entry.get("time_close"),
                eid,
            ),
        )
        conn.commit()
    return get_journal_entry(eid)


def delete_journal_entry(eid):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM journal WHERE id = ?", (eid,))
        conn.commit()


# ============================================================
# Explorador de tablas (solo lectura)
# ============================================================

def list_db_tables():
    with closing(get_db()) as conn:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        out = []
        for t in tables:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{t}')").fetchall()]
            n = conn.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
            out.append({"name": t, "columns": cols, "rows": n})
        return out


def _extract_setup_score(setup_json):
    """Extrae el score de confluencia de setup_json (raíz, breakdown o risk)."""
    if not setup_json:
        return None
    if isinstance(setup_json, str):
        try:
            setup_json = json.loads(setup_json)
        except Exception:
            return None
    if not isinstance(setup_json, dict):
        return None
    for cand in ("score", "setup_score", "score_total"):
        v = setup_json.get(cand)
        if isinstance(v, (int, float)):
            return round(float(v), 1)
    for grp in ("breakdown", "risk", "summary"):
        g = setup_json.get(grp)
        if isinstance(g, dict):
            for cand in ("score", "total", "score_total"):
                v = g.get(cand)
                if isinstance(v, (int, float)):
                    return round(float(v), 1)
    return None


def query_db_table(table, limit=100):
    with closing(get_db()) as conn:
        valid = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if table not in valid:
            return None
        if table == "trades":
            # Vista combinada: trades + contexto de journal por ticket
            cols = [
                "id", "ticket", "symbol", "action", "volume", "price_open",
                "price_close", "profit", "time_close", "synced_at",
                "poi_type", "setup_score", "cme_confirmation",
            ]
            sql = (
                "SELECT t.id, t.ticket, t.symbol, t.action, t.volume, t.price_open, "
                "t.price_close, t.profit, t.time_close, t.synced_at, "
                "j.poi_type, j.setup_json, j.cme_confirmation "
                "FROM trades t "
                "LEFT JOIN journal j ON j.ticket = t.ticket "
                "LIMIT ?"
            )
            raw = conn.execute(sql, (limit,)).fetchall()
            rows = []
            for r in raw:
                setup = _extract_setup_score(r[11])
                rows.append(list(r[:10]) + [r[10], setup, r[12]])
            return {"table": table, "columns": cols, "rows": rows}
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()]
        sql = f"SELECT * FROM '{table}' LIMIT ?"
        rows = [list(r) for r in conn.execute(sql, (limit,)).fetchall()]
        return {"table": table, "columns": cols, "rows": rows}
