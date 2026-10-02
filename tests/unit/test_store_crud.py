"""Round-trip del CRUD de `database/store.py` sobre una base temporal.

Este fichero no prueba lógica de negocio (eso vive en `core/` y ya está cubierto);
prueba que lo que entra en SQLite sale igual, y sobre todo que una fila corrupta
o una columna ausente degradan en vez de tumbar el listado. Esa es la diferencia
entre "no hay evidencia" y un 500 que se lleva por delante el post-mortem entero.

Portado de los tests de REF que exercitan `store` (test_link_setup,
test_json_resilience, test_drawings, test_dry_run_sqlite) sin `app.py`, que es
Fase 6.
"""

from __future__ import annotations

import sqlite3

import pytest

from database import store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    target = tmp_path / "crud.db"
    monkeypatch.setattr(store, "DB_PATH", str(target))
    monkeypatch.setattr(store, "PROJECT_DIR", str(tmp_path))
    store.set_config_source(_Fake())
    store.init_db()
    yield target
    store.set_config_source(None)


class _Fake:
    def get_config(self) -> dict:
        return {"agent_risk_policy": "p"}

    def save_config(self, cfg):  # pragma: no cover - no se usa aquí
        pass

    def data_sources(self) -> dict:
        return {}

    def agent_topics(self) -> dict:
        return {}

    def watcher_config(self) -> dict:
        return {}


def _raw(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# setup_log: el post-mortem
# ---------------------------------------------------------------------------


class TestSetupLog:
    def test_round_trip_completo(self, db):
        sid = store.log_setup(
            {
                "symbol": "eurusd",
                "timeframe": "m15",
                "direction": "buy",
                "verdict": "ALTA",
                "score": 78.5,
                "breakdown": {"smc": 40, "dxy": 15, "cot": 0},
                "entry": 1.0850,
                "sl": 1.0840,
                "target": 1.0870,
                "invalidate_level": 1.0835,
                "validated": True,
                "reject_reasons": [],
                "risk_state": {"equity": 25300.14, "risk_pct": 0.5},
                "trade_result": {},
                "context": {"snapshot_age_s": 12.5, "source": "databento"},
                "source": "",
            }
        )
        assert sid is not None
        rows = store.list_setup_log(limit=10)
        assert len(rows) == 1
        r = rows[0]
        # Símbolo y timeframe se normalizan a mayúsculas al escribir.
        assert r["symbol"] == "EURUSD"
        assert r["timeframe"] == "M15"
        assert r["direction"] == "BUY"
        assert r["verdict"] == "ALTA"
        assert r["score"] == pytest.approx(78.5)
        assert r["breakdown"]["smc"] == 40
        assert r["validated"] == 1
        assert r["risk_state"]["equity"] == pytest.approx(25300.14)
        # snapshot_age_s se promociona a columna propia: es por lo que se filtra.
        assert r["snapshot_age_s"] == pytest.approx(12.5)
        assert r["context"]["source"] == "databento"
        assert r["timestamp"].endswith("+00:00")

    def test_es_append_only(self, db):
        """Cada disparo del motor deja fila propia. Reescribir el pasado
        destruiría la evidencia, así que `log_setup` siempre inserta."""
        store.log_setup({"verdict": "ALTA", "score": 70})
        store.log_setup({"verdict": "BAJA", "score": 30})
        rows = store.list_setup_log(limit=10)
        assert len(rows) == 2
        assert {r["verdict"] for r in rows} == {"ALTA", "BAJA"}

    def test_update_trade_result_no_toca_el_resto(self, db):
        """El desenlace se añade después; lo demás de la fila es inmutable."""
        sid = store.log_setup(
            {"verdict": "ALTA", "score": 78, "breakdown": {"smc": 40}}
        )
        store.update_trade_result(sid, {"result": "WIN", "r_multiple": 1.8})
        row = store.list_setup_log(limit=1)[0]
        assert row["trade_result"]["result"] == "WIN"
        assert row["trade_result"]["r_multiple"] == pytest.approx(1.8)
        assert row["breakdown"]["smc"] == 40
        assert row["score"] == pytest.approx(78)

    def test_filtra_por_verdict(self, db):
        store.log_setup({"verdict": "ALTA", "score": 70})
        store.log_setup({"verdict": "SIN_OPERATIVA", "score": 10})
        altas = store.list_setup_log(limit=10, verdict="ALTA")
        assert len(altas) == 1
        assert altas[0]["verdict"] == "ALTA"

    def test_filtra_por_simbolo(self, db):
        store.log_setup({"symbol": "EURUSD", "verdict": "ALTA"})
        store.log_setup({"symbol": "DAX", "verdict": "ALTA"})
        rows = store.list_setup_log(limit=10, symbol="DAX")
        assert len(rows) == 1
        assert rows[0]["symbol"] == "DAX"

    def test_el_limite_respeta_el_mas_reciente(self, db):
        for i in range(5):
            store.log_setup({"verdict": "ALTA", "score": i})
        rows = store.list_setup_log(limit=2)
        assert len(rows) == 2
        assert rows[0]["id"] > rows[1]["id"], "el más reciente primero"


class TestJsonResiliente:
    """`_safe_json` existe porque `json.loads` LANZA. En un listado eso no pierde
    solo la fila: la excepción sube antes de devolver nada y se llevan por delante
    TODAS las demás. setup_log tiene cinco columnas JSON y el audit depende de
    las cinco."""

    def _corrupt(self, db, column, value):
        conn = _raw(db)
        conn.execute(
            "INSERT INTO setup_log (symbol, timeframe, direction, verdict, score, "
            "breakdown_json, reject_reasons, risk_state_json, trade_result_json, "
            "context_json, source, timestamp) "
            "VALUES ('EURUSD','M15','BUY','ALTA',70,?,?,?,?,?,'','2026-09-01T00:00:00+00:00')",
            (value, value, value, value, value),
        )
        conn.commit()
        conn.close()

    @pytest.mark.parametrize(
        "column", ["breakdown_json", "reject_reasons", "risk_state_json",
                   "trade_result_json", "context_json"],
    )
    def test_una_columna_rota_no_tumba_el_listado(self, db, column):
        """Las filas sanas siguen saliendo; la rota se degrada al default."""
        store.log_setup({"verdict": "ALTA", "score": 70})
        self._corrupt(db, column, "{esto no es json")
        rows = store.list_setup_log(limit=10)
        assert len(rows) == 2, "la fila sana no puede desaparecer"
        defaults = {"breakdown_json": {}, "reject_reasons": [],
                    "risk_state_json": {}, "trade_result_json": {}, "context_json": {}}
        broken = [r for r in rows if r["id"] != max(x["id"] for x in rows)]
        assert broken, "la fila corrupta deberia estar presente"
        assert broken[0][column.replace("_json", "")] == defaults[column]

    def test_json_valido_de_otra_forma_se_normaliza(self, db):
        """Una lista donde se espera un dict rompe igual a quien lo consume, así
        que se normaliza en la lectura y no en cada llamada."""
        store.log_setup({"verdict": "ALTA"})
        self._corrupt(db, "breakdown_json", "[1, 2, 3]")
        rows = store.list_setup_log(limit=10)
        for r in rows:
            assert isinstance(r["breakdown"], dict)


# ---------------------------------------------------------------------------
# roles / conversaciones / mensajes
# ---------------------------------------------------------------------------


class TestAgente:
    def test_upsert_de_rol_es_idempotente(self, db):
        store.upsert_role("risk", "Riesgo", "p1", ["a"], provider="openai", model="m")
        store.upsert_role("risk", "Riesgo", "p2", ["b"], provider="openai", model="m2")
        role = store.get_role("risk")
        assert role["system_prompt"] == "p2"
        assert role["allowed_tools"] == ["b"]
        assert len([r for r in store.list_roles() if r["id"] == "risk"]) == 1

    def test_delete_de_rol(self, db):
        store.upsert_role("risk", "Riesgo", "p", [])
        store.delete_role("risk")
        assert store.get_role("risk") is None

    def test_conversacion_con_mensajes(self, db):
        # create_conversation devuelve la fila recién creada, no el id suelto.
        conv = store.create_conversation("Post-mortem EURUSD")
        cid = conv["id"]
        store.add_message(cid, "user", "¿qué pasó?")
        store.add_message(cid, "assistant", "El spread se comió el SL.")
        got = store.get_conversation(cid)
        assert got["title"] == "Post-mortem EURUSD"
        msgs = store.get_messages(cid, limit=10)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["content"] == "El spread se comió el SL."

    def test_los_mensajes_de_otra_conversacion_no_se_mezclan(self, db):
        """El índice idx_messages_conv es lo que evita esto."""
        a = store.create_conversation("A")["id"]
        b = store.create_conversation("B")["id"]
        store.add_message(a, "user", "de A")
        store.add_message(b, "user", "de B")
        assert [m["content"] for m in store.get_messages(a, limit=10)] == ["de A"]
        assert [m["content"] for m in store.get_messages(b, limit=10)] == ["de B"]

    def test_la_traza_de_herramientas_se_persiste_pero_no_se_expone(self, db):
        """Dos comportamientos a proposito, y conviene no confundirlos.

        La fila se guarda con `name` y `tool_call_id` (sin eso no se puede
        reconstruir qué herramienta tocó qué). Pero `get_messages` FILTRA las
        filas role='tool': existen solo como traza de debug, porque devolverlas
        sin el `tool_calls` del assistant emparejado dejaría mensajes huérfanos y
        rompería el contrato de la API del agente.
        """
        cid = store.create_conversation("tools")["id"]
        store.add_message(
            cid, "tool", '{"status":"ok"}', tool_call_id="call_1", name="economic_news"
        )
        # 1. Persistido: se lee directo de la tabla.
        conn = _raw(db)
        row = conn.execute(
            "SELECT role, name, tool_call_id FROM messages WHERE conversation_id=?",
            (cid,),
        ).fetchone()
        conn.close()
        assert row["name"] == "economic_news"
        assert row["tool_call_id"] == "call_1"

        # 2. No expuesto por el lector del agente.
        assert store.get_messages(cid, limit=10) == []

    def test_delete_de_conversacion(self, db):
        cid = store.create_conversation("x")["id"]
        store.delete_conversation(cid)
        assert store.get_conversation(cid) is None


# ---------------------------------------------------------------------------
# setup_state / drawings (el watcher)
# ---------------------------------------------------------------------------


class TestSetupState:
    def test_dedup_por_symbol_timeframe(self, db):
        """El watcher no debe avisar dos veces del mismo setup."""
        store.new_setup_state("EURUSD", "M15", gate={"score": 80, "verdict": "ALTA"})
        store.new_setup_state("EURUSD", "M15", gate={"score": 85, "verdict": "ALTA"})
        states = store.list_setup_states()
        assert len([s for s in states if s["symbol"] == "EURUSD"]) == 1

    def test_distinto_timeframe_es_otra_fila(self, db):
        store.new_setup_state("EURUSD", "M15")
        store.new_setup_state("EURUSD", "M5")
        assert len(store.list_setup_states()) == 2

    def test_el_gate_se_despliega_en_columnas(self, db):
        """El gate llega como dict y se separa en columnas para poder filtrar."""
        store.new_setup_state(
            "EURUSD",
            "M15",
            gate={
                "direction": "BUY", "score": 82.0, "verdict": "ALTA",
                "entry": 1.085, "sl": 1.084, "tp": 1.087,
                "invalidate_level": 1.0835,
            },
        )
        state = store.get_setup_state("EURUSD", "M15")
        assert state["direction"] == "BUY"
        assert state["score"] == pytest.approx(82.0)
        assert state["entry"] == pytest.approx(1.085)

    def test_update_marca_notificado(self, db):
        store.new_setup_state("EURUSD", "M15", notified=False)
        assert store.get_setup_state("EURUSD", "M15")["notified"] == 0
        store.update_setup_state("EURUSD", "M15", notified=True)
        assert store.get_setup_state("EURUSD", "M15")["notified"] == 1

    def test_auto_execute_del_watcher(self, db):
        """El override de la UI manda sobre el YAML."""
        assert store.get_watcher_auto_execute() is False
        store.set_watcher_auto_execute(True)
        assert store.get_watcher_auto_execute() is True


class TestDrawings:
    def test_round_trip_con_origen(self, db):
        """Cada dibujo lleva origin user|agent: sin eso no se sabe quién dibujó."""
        items = [{"id": "r1", "type": "hline", "price": 1.0850, "origin": "user"}]
        store.save_drawings("EURUSD", "M15", items)
        got = store.get_drawings("EURUSD", "M15")
        assert len(got) == 1
        assert got[0]["origin"] == "user"
        assert got[0]["price"] == pytest.approx(1.0850)

    def test_sobrescribe_por_symbol_timeframe(self, db):
        store.save_drawings("EURUSD", "M15", [{"id": "a", "origin": "user"}])
        store.save_drawings("EURUSD", "M15", [{"id": "b", "origin": "agent"}])
        got = store.get_drawings("EURUSD", "M15")
        assert [d["id"] for d in got] == ["b"]

    def test_otro_timeframe_no_se_pisa(self, db):
        store.save_drawings("EURUSD", "M15", [{"id": "a", "origin": "user"}])
        store.save_drawings("EURUSD", "M5", [{"id": "b", "origin": "user"}])
        assert len(store.get_drawings("EURUSD", "M15")) == 1
        assert len(store.get_drawings("EURUSD", "M5")) == 1

    def test_borrar(self, db):
        store.save_drawings("EURUSD", "M15", [{"id": "a", "origin": "user"}])
        store.delete_drawings("EURUSD", "M15")
        assert store.get_drawings("EURUSD", "M15") == []

    def test_vacio_no_rompe(self, db):
        """Un listado sin dibujos es `[]`, no None: la UI lo recorre."""
        assert store.get_drawings("DAX", "M15") == []


# ---------------------------------------------------------------------------
# alertas / COT / prop_state / trades / journal
# ---------------------------------------------------------------------------


class TestChartAlerts:
    def test_alerta_con_condiciones(self, db):
        # add_chart_alert devuelve la fila ya creada, no el id suelto.
        alert = store.add_chart_alert(
            "eurusd", 1.0850, label="resistencia", side="above",
            conditions=[{"type": "close_above", "price": 1.0850}],
            timeframe="M15",
        )
        assert alert["symbol"] == "EURUSD", "el símbolo se normaliza a mayúsculas"
        assert alert["label"] == "resistencia"
        assert alert["conditions"][0]["type"] == "close_above"
        assert alert["timeframe"] == "M15"
        assert alert["status"] == "active"

    def test_disparar_marca_el_sello(self, db):
        alert = store.add_chart_alert("EURUSD", 1.0850, label="x")
        store.set_chart_alert_status(alert["id"], "triggered")
        got = store.get_chart_alert(alert["id"])
        assert got["status"] == "triggered"
        assert got["triggered_at"] is not None

    def test_solo_las_activas_por_defecto(self, db):
        a = store.add_chart_alert("EURUSD", 1.0850)["id"]
        store.add_chart_alert("DAX", 18000.0)
        store.set_chart_alert_status(a, "triggered")
        assert len(store.list_chart_alerts(active_only=True)) == 1
        assert len(store.list_chart_alerts(active_only=False)) == 2


class TestCotReports:
    def test_upsert_por_fecha(self, db):
        """El informe COT es semanal y se re-publica: la fecha es la clave."""
        store.upsert_cot_report("2026-09-01", 263253, -38173, -24925, 26.8, "NEUTRAL")
        store.upsert_cot_report("2026-09-01", 270000, -40000, -25000, 30.0, "BULLISH")
        reports = store.list_cot_reports(limit=10)
        assert len(reports) == 1
        assert reports[0]["macro_bias"] == "BULLISH"

    def test_el_ultimo_es_el_mas_reciente(self, db):
        store.upsert_cot_report("2026-08-25", 1, 2, 3, 20.0, "NEUTRAL")
        store.upsert_cot_report("2026-09-01", 1, 2, 3, 26.8, "BULLISH")
        latest = store.latest_cot_report()
        assert latest["report_date"] == "2026-09-01"


class TestPropState:
    def test_baseline_y_picos(self, db):
        """El gate de prop-firm necesita el baseline equity para medir el DD."""
        store.set_prop_state(baseline_equity=25300.14, baseline_day="2026-09-25")
        state = store.get_prop_state()
        assert state["baseline_equity"] == pytest.approx(25300.14)
        assert state["baseline_day"] == "2026-09-25"

    def test_actualizar_no_borra_las_otras_claves(self, db):
        store.set_prop_state(baseline_equity=1000.0)
        store.set_prop_state(day_peak=1200.0)
        state = store.get_prop_state()
        assert state["baseline_equity"] == pytest.approx(1000.0)
        assert state["day_peak"] == pytest.approx(1200.0)


class TestTrades:
    def test_upsert_y_listar(self, db):
        store.upsert_trades(
            [
                {"ticket": 10371608506, "symbol": "EURUSD", "action": "BUY",
                 "profit": -28.98, "time_close": "2026-09-20 10:00:00+00:00"},
            ]
        )
        rows = store.list_trades(symbol="EURUSD")
        assert len(rows) == 1
        assert rows[0]["ticket"] == 10371608506
        assert rows[0]["profit"] == pytest.approx(-28.98)

    def test_clear_vacia_el_espejo(self, db):
        """`trades` es un espejo del broker: se reconstruye en cada sync."""
        store.upsert_trades(
            [{"ticket": 1, "symbol": "EURUSD", "action": "BUY", "profit": 1.0}]
        )
        store.clear_trades()
        assert store.list_trades() == []

    def test_reexportar_el_mismo_ticket_no_duplica(self, db):
        """El UNIQUE de `ticket` es lo que evita contar dos veces la misma
        operación si el sync corre dos veces."""
        row = {"ticket": 42, "symbol": "EURUSD", "action": "SELL", "profit": 10.0}
        store.upsert_trades([row])
        store.upsert_trades([row])
        assert len(store.list_trades()) == 1


class TestJournal:
    def test_round_trip(self, db):
        # La columna se llama setup_json y `add_journal_entry` devuelve la fila.
        entry = store.add_journal_entry(
            {
                "symbol": "EURUSD",
                "action": "BUY",
                "poi_type": "FVG",
                "liquidity_swept": "yes",
                "setup_json": {"direction": "BUY", "score": 78},
                "emotion": "calma",
                "plan_compliance": 1,
                "tags": ["6e", "smc"],
                "notes": "Entrada tarde por el spread.",
                "entry_price": 1.0850,
                "sl_price": 1.0840,
                "tp_price": 1.0870,
            }
        )
        assert entry["symbol"] == "EURUSD"
        assert entry["poi_type"] == "FVG"
        assert entry["tags"] == ["6e", "smc"]
        assert entry["plan_compliance"] == 1
        assert entry["entry_price"] == pytest.approx(1.0850)
        assert entry["timestamp"].endswith("+00:00")

    def test_el_setup_json_va_parseado(self, db):
        entry = store.add_journal_entry(
            {"symbol": "EURUSD", "setup_json": {"direction": "BUY", "score": 78}}
        )
        assert entry["setup_json"]["direction"] == "BUY"
        assert entry["setup_json"]["score"] == pytest.approx(78)

    def test_el_simbolo_por_defecto_es_eurusd(self, db):
        assert store.add_journal_entry({"action": "BUY"})["symbol"] == "EURUSD"

    def test_init_db_sobrevive_sin_config_y_avisa(self, db):
        """Crear el esquema no puede depender de `strategy` (que es de la Fase 6).

        El prompt del rol sale del YAML, así que sembrarlo sin config es
        imposible... pero que eso tumbe `init_db()` sería al revés de lo
        razonable: el esquema tiene que existir ANTES de que haya estrategia.
        Se comprueban las tres cosas: que noPetna, que avisa, y que la fila
        existe igual para poder rellenarla más adelante.
        """
        store.set_config_source(None)  # el default lazy: strategy no existe

        with pytest.warns(RuntimeWarning, match="prompt"):
            store.init_db()

        conn = _raw(db)
        row = conn.execute(
            "SELECT id, system_prompt, active FROM roles WHERE id = 'general'"
        ).fetchone()
        conn.close()
        assert row is not None, "el rol debe sembrarse aunque sea sin prompt"
        assert row["system_prompt"] == ""
        assert row["active"] == 1

    def test_un_yaml_roto_no_se_disfraza_de_config_ausente(self, db):
        """`ConfigUnavailable` es solo para config AUSENTE, no para config ROTA.

        Si `strategy` existiera pero su YAML estuviera corrupto, el error real
        (un YAMLDecodeError, o el error de validación que sea) tiene que llegar
        intacto al caller. Tragar cualquier excepción aquí convertiría un bug
        visible en un prompt de rol vacío silencioso.
        """
        class ConfigQuebrada:
            def get_config(self):
                raise ValueError("YAML corrupto en la linea 42")

        store.set_config_source(ConfigQuebrada())
        with pytest.raises(ValueError, match="YAML corrupto"):
            store.init_db()

    def test_update_es_put_no_patch(self, db):
        """`update_journal_entry` REEMPLAZA la fila entera, como el PUT de la API.

        Se prueba a proposito el comportamiento que sorprende: una clave que no
        viene en el dict se pierde. Documentarlo aqui es lo que evita que el
        siguiente que llame a esta función con un dict parcial descubra que ha
        borrado las notas del post-mortem.
        """
        entry = store.add_journal_entry(
            {"symbol": "EURUSD", "action": "BUY", "notes": "original"}
        )
        store.update_journal_entry(entry["id"], {"emotion": "duda"})
        got = store.get_journal_entry(entry["id"])
        assert got["emotion"] == "duda"
        assert got["notes"] is None, "PUT: lo que no viene, se borra"

    def test_update_con_el_dict_completo_conserva_todo(self, db):
        """El ciclo real: leer la fila, tocarla y volver a guardarla."""
        entry = store.add_journal_entry(
            {
                "symbol": "EURUSD",
                "action": "BUY",
                "notes": "Entrada tarde por el spread.",
                "setup_json": {"direction": "BUY"},
                "tags": ["smc"],
            }
        )
        store.update_journal_entry(entry["id"], dict(entry, emotion="duda"))
        got = store.get_journal_entry(entry["id"])
        assert got["emotion"] == "duda"
        assert got["notes"] == "Entrada tarde por el spread."
        assert got["setup_json"]["direction"] == "BUY"
        assert got["tags"] == ["smc"]

    def test_listar_por_simbolo(self, db):
        store.add_journal_entry({"symbol": "EURUSD", "action": "BUY"})
        store.add_journal_entry({"symbol": "DAX", "action": "SELL"})
        assert len(store.list_journal(symbol="DAX")) == 1


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_set_es_upsert(self, db):
        store.set_setting("history_limit", "50")
        assert store.get_settings()["history_limit"] == "50"

    def test_una_clave_inexistente_no_aparece(self, db):
        """No hay `get_setting`: se leen todas y se mira el dict. Una clave
        ausente no es un error, es que no está puesta todavía."""
        store.set_setting("history_limit", "50")
        assert "no_existe" not in store.get_settings()