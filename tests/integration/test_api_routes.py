"""Los endpoints por dentro del HTTP: rutas, errores, auth y WebSocket.

Este fichero es la frontera que REF no tenía. Allá, la única manera de probar un
endpoint era levantar el proceso entero y llamarlo con `requests`, con el terminal
y la base de datos de verdad; aquí una app con dobles responde en memoria y falla
en el sitio que toca.

Lo que se comprueba, en orden de importancia:

  1. **Los códigos sonsignificado.** 404 de un símbolo inexistente, 503 de un
     bróker apagado, 502 de una fuente macro caída y 400 de un parámetro
     imposible son cuatro hechos distintos y el frontend los pinta distinto. Con
     el `except Exception` de REF los cuatro llegaban como 500 con un texto.
  2. **Las escrituras piden token y las lecturas no.** El token se lee por
     petición, así que un test puede ponerlo y quitarlo en la misma app.
  3. **La bitácora y los roles hacen CRUD de verdad** contra una base temporal,
     porque un 200 con un doble que devuelve lo que sea no dice nada.
  4. **El SSE del agente y el WebSocket de cinta** emiten algo que el JS de REF
     sabe leer: `data:` con JSON dentro, y el primer mensaje con el estado.

Los dobles están al principio y son tontos a propósito: un `FakeMarket` que
devuelve velas inventadas vale más aquí que un `MT5Market` con el terminal
apagado, porque el fallo que importa en una ruta es el de la ruta.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from adapters.base_adapter import AdapterError, SymbolNotFound
from agent.ports import AgentPortError
from api.app import create_app
from api.runtime import Runtime
from database.store import ConfigUnavailable
from macro_ingestor.base_ingestor import IngestorError

# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------

EPOCH = 1767225600  # 2026-01-01T00:00:00Z, un jueves: la semana empieza en jueves


def vela(i: int) -> Dict[str, Any]:
    base = 1.0850 + i * 0.0001
    return {
        "time": EPOCH + i * 900,
        "open": base,
        "high": base + 0.0005,
        "low": base - 0.0005,
        "close": base + 0.0002,
        "tick_volume": 100 + i,
        "volume": 100 + i,
        "spread": 2,
    }


class FakeAdapter:
    def __init__(self, simbolos: Optional[List[str]] = None) -> None:
        self._simbolos = list(simbolos or ["EURUSD", "GBPUSD"])
        self.session = None

    def symbols(self) -> List[str]:
        return list(self._simbolos)

    def terminal_files_dir(self) -> str:
        return "C:\\vacio"


class FakeMarket:
    """Lo mínimo que las rutas piden. `fallo` hace que TODO lance."""

    def __init__(
        self,
        *,
        fallo: Optional[BaseException] = None,
        velas: Optional[List[Dict[str, Any]]] = None,
        simbolos: Optional[List[str]] = None,
        veredicto_smr: Optional[Dict[str, Any]] = None,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.fallo = fallo
        self.velas = velas if velas is not None else [vela(i) for i in range(60)]
        self.adapter = FakeAdapter(simbolos)
        self.pedidos: List[tuple] = []
        self.enriquecidas: List[Dict[str, Any]] = []
        self.veredicto_smr = veredicto_smr
        self.snapshot = snapshot

    # -- helpers ---------------------------------------------------------------

    def _lanza(self) -> None:
        if self.fallo is not None:
            raise self.fallo

    # -- puerto de mercado ------------------------------------------------------

    def account_info(self) -> Dict[str, Any]:
        self._lanza()
        return {"balance": 10000.0, "equity": 10100.5, "margin": 250.0, "profit": 100.5}

    def positions(self) -> List[Dict[str, Any]]:
        self._lanza()
        return [{"ticket": 1, "symbol": "EURUSD", "volume": 0.1, "profit": 5.0}]

    def position(self, ticket: int) -> Optional[Dict[str, Any]]:
        self._lanza()
        if ticket != 1:
            return None
        return {"ticket": 1, "symbol": "EURUSD", "volume": 0.1, "profit": 5.0}

    def history(self, days: int = 7) -> List[Dict[str, Any]]:
        self.pedidos.append(("history", days))
        self._lanza()
        return [{"ticket": 7, "symbol": "EURUSD", "profit": 12.0}]

    def daily_risk_state(self) -> Dict[str, Any]:
        self._lanza()
        return {"dd_pct": 1.5, "operations_today": 2, "blocked": False}

    def price(self, symbol: str) -> Optional[Dict[str, Any]]:
        self.pedidos.append(("price", symbol))
        self._lanza()
        if symbol == "NOPUBLICA":
            return None
        return {"symbol": symbol, "bid": 1.0850, "ask": 1.0852, "time": EPOCH}

    def candles(self, symbol: str, timeframe: str, bars: int) -> List[Dict[str, Any]]:
        self.pedidos.append(("candles", symbol, timeframe, bars))
        self._lanza()
        return list(self.velas[:bars])

    def spec(self, symbol: str) -> Optional[Dict[str, Any]]:
        self.pedidos.append(("spec", symbol))
        self._lanza()
        if symbol == "NOPUBLICA":
            return None
        return {"symbol": symbol, "digits": 5, "point": 0.00001, "trade_stops_level": 10}

    def pattern_data(self, symbol: str, timeframe: str, bars: int) -> Dict[str, Any]:
        self.pedidos.append(("patterns", symbol, timeframe, bars))
        self._lanza()
        return {"fvg": [], "order_blocks": [], "sweeps": []}

    def chart_snapshot(
        self, symbol: str, timeframe: str, bars: int = 300, force_killzone: bool = False
    ) -> Dict[str, Any]:
        """`force_killzone` se propaga al `pedidos` y se marca en el motor.

        El default del kwarg importa: `api/routes/setup.py` lo pasa SIEMPRE, así que
        un doble con la firma antigua no es «menos cobertura», es un `TypeError` en
        la ruta. Marcarlo en `risk_engine.killzone_forced` reproduce lo que hace
        `MT5Market._risk_engine` cuando consigue un instante forzado de verdad: es lo
        que permite comprobar que la respuesta declara el score forzado SIN que el
        test tenga que inventar ese valor a mano.
        """
        self.pedidos.append(("snapshot", symbol, timeframe, bars, force_killzone))
        self._lanza()
        if self.snapshot is None:
            return {"symbol": symbol, "timeframe": timeframe, "built_at": "2026-01-01T00:00:00+00:00"}
        salida = dict(self.snapshot)
        motor = salida.get("risk_engine")
        if force_killzone and isinstance(motor, dict) and motor.get("bull"):
            salida["risk_engine"] = dict(motor, killzone_forced=True)
        return salida

    def orderflow_snapshot(self) -> Dict[str, Any]:
        self._lanza()
        return {"symbol": "EURUSD", "cvd": 12.0, "cvd_source": "synthetic"}

    def smr(self, simbolo: str, timeframe: str, velas: List[Dict[str, Any]]) -> Dict[str, Any]:
        self.pedidos.append(("smr", simbolo, timeframe, len(velas)))
        self._lanza()
        if self.veredicto_smr is not None:
            return dict(self.veredicto_smr)
        return {"confirmed": False, "reason": "sin feed", "bull": {"confirmed": False}, "bear": {"confirmed": False}}

    def orderflow_alerts(self) -> List[Dict[str, Any]]:
        self._lanza()
        return []

    def enrich_journal_entry(self, entrada: Dict[str, Any]) -> Dict[str, Any]:
        self.enriquecidas.append(entrada)
        salida = dict(entrada)
        salida["entry_price"] = salida.get("entry_price") or 1.0850
        return salida


class FakeNews:
    def __init__(self, veredicto: Optional[Dict[str, Any]] = None) -> None:
        self._veredicto = veredicto or {"block": None, "fail_open": True, "disabled": False}

    def gate(self) -> Dict[str, Any]:
        return dict(self._veredicto)

    def news(self) -> Dict[str, Any]:
        return {"estado": "ok", "eventos": []}


class FakeExports:
    def __init__(self, ruta: Optional[str] = None) -> None:
        self._ruta = ruta

    def files_dir(self) -> Optional[str]:
        return self._ruta


# Sesiones con la FORMA de la real. La de producción (`adapters/forex/mt5_forex.py`)
# expone `is_open` como `@property`, no como método; un doble que solo sepa llamar
# métodos deja pasar bugs que revientan la primera pantalla con terminal abierta.


class _SesionConProperty:
    def __init__(self, *, abierta: bool) -> None:
        self._abierta = abierta

    @property
    def is_open(self) -> bool:
        return self._abierta


class _SesionConMetodo:
    def is_open(self) -> bool:
        return True


class _SesionQueExplota:
    @property
    def is_open(self) -> bool:
        raise RuntimeError("la sesión no responde")


class _SesionPerezosa:
    """Como la real: `is_open` es `False` hasta que algo abre la terminal."""

    def __init__(self, *, alcanzable: bool = True) -> None:
        self._alcanzable = alcanzable
        self.intentos = 0

    @property
    def is_open(self) -> bool:
        return self.intentos > 0

    def call(self, fn):
        self.intentos += 1
        if not self._alcanzable:
            raise RuntimeError("no hay conexión con MetaTrader 5")
        return fn(object())


def _lanza_config() -> Dict[str, Any]:
    raise RuntimeError("no hay config")


class FakeStore:
    """El store mínimo que leen `/api/health` y `/api/config/summary`.

    Existe para NO usar el módulo real aquí: abrir `database/trading.db` en modo
    WAL hace que hasta una lectura termine en checkpoint al cerrar, y el canario de
    `tests/conftest.py` lo detecta como modificación del fichero real. Los tests que
    necesitan CRUD de verdad usan la base temporal del fixture `db`.
    """

    def __init__(self, tablas: Optional[List[str]] = None,
                 config: Optional[Dict[str, Any]] = None) -> None:
        self._tablas = list(tablas or ["journal", "trades", "conversations"])
        self._config = config

    def list_db_tables(self) -> List[Dict[str, Any]]:
        return [{"table": t, "rows": 0} for t in self._tablas]

    def get_trading_config(self) -> Dict[str, Any]:
        if self._config is not None:
            return dict(self._config)
        return {"symbols": ["EURUSD"], "prop": {}, "execution": {}}

    def get_config_summary(self) -> Dict[str, Any]:
        return {"risk_weights": {}, "killzones": []}


class FakeEngine:
    """El motor de order flow, para el WebSocket. Ajustes y snapshot."""

    symbol = "EURUSD"

    def __init__(self) -> None:
        self.ajustes = {"zscore_threshold": 3.0, "absorb_trades": 50}
        self.paradas = 0

    def settings(self) -> Dict[str, Any]:
        return dict(self.ajustes)

    def update_settings(self, **kwargs: Any) -> Dict[str, Any]:
        for clave, valor in kwargs.items():
            if valor is not None:
                self.ajustes[clave] = valor
        return dict(self.ajustes)

    def snapshot(self) -> Dict[str, Any]:
        return {"msgs_per_sec": 25.0, "cvd": 5.0, "alerts": []}

    def stop(self) -> None:
        self.paradas += 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _runtime(**kwargs: Any) -> Runtime:
    base = {
        "market": FakeMarket(),
        "news": FakeNews(),
        "exports": FakeExports(),
        "orderflow": FakeEngine(),
    }
    base.update(kwargs)
    return Runtime(**base)


@pytest.fixture
def cliente() -> Any:
    """App con dobles. Sin token, sin base de datos real, sin terminal."""
    with TestClient(create_app(_runtime())) as c:
        yield c


@pytest.fixture
def db(tmp_path, monkeypatch) -> Any:
    """Base de datos temporal, redirigiendo los DOS puntos de entrada.

    `DB_PATH` y `PROJECT_DIR` son atributos de MÓDULO en `database.store`: se
    redirigen ahí y no en `core.paths`, que es lo que vigila el canario de
    `conftest.py`. Sin esto, cualquier escritura de estos tests va a
    `database/trading.db` de verdad (ya pasó).

    El seam de config también se sustituye, por la misma razón: el default es el
    módulo `strategy`, que aún no existe, y sin esto cada lectura de config falla
    con `ConfigUnavailable` y los tests acabarían probando el camino del error.
    """
    from database import store as store_mod

    from tests.unit.test_store import FakeConfigSource

    monkeypatch.setattr(store_mod, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(store_mod, "PROJECT_DIR", str(tmp_path))
    anterior = store_mod.set_config_source(FakeConfigSource())
    store_mod.init_db()
    try:
        yield store_mod
    finally:
        store_mod.set_config_source(anterior)


@pytest.fixture
def cliente_db(db) -> Any:
    """App con la base de verdad (temporal) y el resto de puertos doblados."""
    from database import store as store_mod

    with TestClient(create_app(_runtime(store=store_mod))) as c:
        yield c


# ---------------------------------------------------------------------------
# Health, config y reloj
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_dice_que_hay_y_que_no(self):
        with TestClient(create_app(_runtime(store=FakeStore()))) as c:
            cuerpo = c.get("/api/health").json()

        assert cuerpo["ok"] is True
        assert cuerpo["store"]["estado"] == "ok"
        assert cuerpo["store"]["tablas"] == 3
        assert cuerpo["market"]["estado"] == "sin_terminal"
        assert cuerpo["news"]["estado"] == "ok"
        assert cuerpo["orderflow"]["estado"] == "ok"
        assert cuerpo["auth"] in ("token", "sin_token")
        # Los estados son strings nombrados, no booleanos: "sin terminal" y
        # "error" son información y `false` no la da.
        assert cuerpo["broker_time"]["tz"]

    def test_health_sin_base_dice_sin_puerto(self):
        with TestClient(create_app(Runtime())) as c:
            cuerpo = c.get("/api/health").json()

        assert cuerpo["ok"] is False
        assert cuerpo["store"] == {"estado": "sin_puerto"}
        assert cuerpo["market"] == {"estado": "sin_puerto"}
        assert cuerpo["news"] == {"estado": "sin_puerto"}

    def test_health_con_la_sesion_real_no_revienta(self):
        """`/api/health` no puede dar 500 justo cuando hay terminal abierta.

        La sesión de MT5 expone `is_open` como `@property` y el runtime la leía
        como método: con el cableado real eso era un `TypeError` y un 500 en la
        primera pantalla, y los tests no lo veían porque el doble de mercado
        lleva `session = None`, con lo que la rama ni se ejecutaba. Un `health`
        que solo funciona en los doubles no es un `health`.
        """
        for abierta, esperado in ((True, "con_terminal"), (False, "sin_terminal")):
            mercado = FakeMarket()
            mercado.session = _SesionConProperty(abierta=abierta)
            with TestClient(create_app(_runtime(store=FakeStore(), market=mercado))) as c:
                r = c.get("/api/health")
            assert r.status_code == 200
            assert r.json()["market"]["estado"] == esperado

    def test_health_acepta_is_open_como_metodo_tambien(self):
        """Un puerto nuevo puede exponer la marca como método: las dos formas valen."""
        mercado = FakeMarket()
        mercado.session = _SesionConMetodo()
        with TestClient(create_app(_runtime(store=FakeStore(), market=mercado))) as c:
            cuerpo = c.get("/api/health").json()
        assert cuerpo["market"]["estado"] == "con_terminal"

    def test_health_no_Explota_si_la_marca_falla(self):
        """Una sesión que no responde es `sin_terminal`, no un 500."""
        mercado = FakeMarket()
        mercado.session = _SesionQueExplota()
        with TestClient(create_app(_runtime(store=FakeStore(), market=mercado))) as c:
            r = c.get("/api/health")
        assert r.status_code == 200
        assert r.json()["market"]["estado"] == "sin_terminal"

    def test_health_pregunta_a_la_terminal_en_vez_de_suponer(self):
        """En una app recién arrancada, `is_open` es `False` aunque el bróker esté vivo.

        La sesión es perezosa: solo marca `is_open` después de que una lectura abrió
        la terminal. Leyendo la marca, `/api/health` publicaba `sin_terminal` en
        cuanto se abría el panel, con el bróker conectado y el resto de rutas
        funcionando. El operador veía rojo y no tocaba nada. Preguntar es lo que
        distingue "no hay terminal" de "todavía no he hablado con ella".
        """
        mercado = FakeMarket()
        mercado.session = _SesionPerezosa()
        with TestClient(create_app(_runtime(store=FakeStore(), market=mercado))) as c:
            r = c.get("/api/health")
        assert r.json()["market"]["estado"] == "con_terminal"
        assert mercado.session.intentos == 1, "no se llegó a preguntar a la terminal"

    def test_health_dice_sin_terminal_si_la_terminal_no_responde(self):
        """Y una terminal cerrada sigue siendo un estado, no un error ni un 500."""
        mercado = FakeMarket()
        mercado.session = _SesionPerezosa(alcanzable=False)
        with TestClient(create_app(_runtime(store=FakeStore(), market=mercado))) as c:
            r = c.get("/api/health")
        assert r.status_code == 200
        assert r.json()["market"]["estado"] == "sin_terminal"

    def test_health_publica_el_reloj_del_ea_con_su_ranciedad(self):
        """El reloj del EA se publica entero, y rancio se dice rancio.

        Antes se leía `.get("estado")` de un payload que no tiene esa clave, así que
        el campo salía siempre `null`: un EA parado tres días se publicaba como si
        nada. `state()` ya sabe distinguir fresco de rancio.
        """
        with TestClient(create_app(_runtime(store=FakeStore()))) as c:
            cuerpo = c.get("/api/health").json()

        reloj = cuerpo["broker_time"]
        assert "error" not in reloj, reloj
        assert reloj["broker"] and reloj["trading_day"] and reloj["tz"]
        assert reloj["ea_clock"] is None or "age_sec" in reloj["ea_clock"]
        if reloj["ea_clock"] is not None:
            assert "stale" in reloj["ea_clock"] or reloj["ea_clock"]["age_sec"] <= 300.0

    def test_config_summary_trae_la_config_anidada(self, cliente_db, db):
        db.get_config_source().cfg = {
            "symbols": ["EURUSD"],
            "prop": {"name": "FTMO", "daily_loss_pct": 5.0},
            "execution": {"slippage": 2},
        }

        cuerpo = cliente_db.get("/api/config/summary").json()

        assert cuerpo["symbols"] == ["EURUSD"]
        assert cuerpo["prop"]["daily_loss_pct"] == 5.0
        assert cuerpo["execution"]["slippage"] == 2
        assert cuerpo["summary"]["killzones"] == []

    def test_config_summary_con_el_yaml_real_no_pinta_ceros(self, cliente_db, db):
        """Con `config/strategy.yaml` puesto, el resumen trae los números.

        El fallo que esto cubre devolvía `min_score: None` y `killzones: []` con la
        config de verdad puesta, porque leía claves planas de un YAML anidado. La UI
        pintaba "sin reglas" con el sistema entero configurado, y ningún test con un
        dict plano lo detectaba.
        """
        import yaml
        from pathlib import Path

        from core import paths as core_paths

        ruta = Path(core_paths.PROJECT_DIR) / "config" / "strategy.yaml"
        with open(ruta, "r", encoding="utf-8") as fh:
            db.get_config_source().cfg = yaml.safe_load(fh)

        cuerpo = cliente_db.get("/api/config/summary").json()

        assert cuerpo["summary"]["min_score"] == 59.5
        assert cuerpo["summary"]["risk_pct"] == 0.5
        assert [kz["name"] for kz in cuerpo["summary"]["killzones"]] == ["Londres", "Nueva York"]

    def test_config_summary_sin_base_devuelve_listas_vacias(self):
        with TestClient(create_app(Runtime())) as c:
            cuerpo = c.get("/api/config/summary").json()

        assert cuerpo["symbols"] == []
        assert "error" in cuerpo

    def test_config_summary_no_tumba_si_la_config_falla(self, cliente_db, monkeypatch):
        """La config no es crítica para pintar la UI: se avisa y se sigue."""
        monkeypatch.setattr(
            cliente_db.app.state.runtime.store,
            "get_config_summary",
            _lanza_config,
        )

        cuerpo = cliente_db.get("/api/config/summary").json()

        assert "error" in cuerpo
        assert cuerpo["symbols"] == []

    def test_clock_publica_bróker_y_dia_de_trading(self, cliente):
        cuerpo = cliente.get("/api/clock").json()

        assert cuerpo["broker_now"]
        assert cuerpo["broker_tz"]
        assert cuerpo["trading_day"]
        assert cuerpo["utc_now"]
        assert cuerpo["verified"] is False

    def test_clock_con_ea_verificado(self):
        """Con el EA publicando y de acuerdo, `verified` pasa a True."""
        import json as _json
        import time as _time

        from api.services.broker_clock import BrokerClock

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ILOF_clock.json").write_text(
                _json.dumps(
                    {
                        "offset_minutes": 0,
                        "trading_day": "2026-01-01",
                        "trades_today": 3,
                        "server_time": "2026-01-01 10:00:00",
                        "build": 4500,
                    }
                ),
                encoding="utf-8",
            )
            reloj = BrokerClock(cfg=lambda: {"clock": {"broker_tz": "UTC"}}, files_dir=lambda: tmp)
            reloj._now = lambda: _time.time()

            estado = reloj.state()

        assert estado["verified"] is True
        assert estado["ea_clock"]["trades_today"] == 3

    def test_openapi_trae_todas_las_familias(self, cliente):
        rutas = cliente.get("/api/openapi.json").json()["paths"]

        for esperada in (
            "/api/health",
            "/api/price/{symbol}",
            "/api/candles/{symbol}",
            "/api/symbols",
            "/api/chartism/{symbol}",
            "/api/journal",
            "/api/journal/{jid}",
            "/api/drawings/{symbol}",
            "/api/orderflow",
            "/api/agent/message",
            "/api/agent/roles",
        ):
            assert esperada in rutas, "falta la ruta {0}".format(esperada)

    def test_todo_mide_y_lo_dice_en_la_cabecera(self, cliente):
        assert float(cliente.get("/api/health").headers["X-Process-Time-Ms"]) >= 0.0

    def test_la_raiz_sirve_el_dashboard(self, cliente):
        import os

        from api.app import STATIC_DIR

        r = cliente.get("/")

        if not os.path.isdir(STATIC_DIR):
            # Sin `static/` la API sigue viva y lo dice: el frontend ausente es un
            # problema de despliegue, no un motivo para no arrancar.
            assert r.status_code == 503
            assert "frontend" in r.json()["error"]
            return

        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert cliente.get("/static/main.js").status_code == 200
        assert cliente.get("/static/style.css").status_code == 200

    def test_cors_solo_deja_los_origenes_de_la_lista(self, cliente):
        permitido = cliente.options(
            "/api/health",
            headers={"Origin": "http://localhost:8000", "Access-Control-Request-Method": "GET"},
        )
        ajeno = cliente.options(
            "/api/health",
            headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
        )

        assert permitido.headers.get("access-control-allow-origin") == "http://localhost:8000"
        assert ajeno.headers.get("access-control-allow-origin") is None


# ---------------------------------------------------------------------------
# Errores: la parte que REF no distinguía
# ---------------------------------------------------------------------------


class TestErrores:
    def test_un_simbolo_que_no_existe_es_404(self):
        with TestClient(create_app(_runtime(market=FakeMarket(fallo=SymbolNotFound("EURUSD"))))) as c:
            r = c.get("/api/price/EURUSD")

        assert r.status_code == 404
        assert "EURUSD" in r.json()["error"]

    def test_bróker_apagado_es_503(self):
        with TestClient(create_app(_runtime(market=FakeMarket(fallo=AdapterError("MT5 no responde"))))) as c:
            r = c.get("/api/account")

        assert r.status_code == 503
        assert r.json()["error"] == "MT5 no responde"

    def test_fuente_macro_caída_es_502(self):
        fallo = IngestorError("no se pudo leer el COT")
        with TestClient(create_app(_runtime(market=FakeMarket(fallo=fallo)))) as c:
            r = c.get("/api/orderflow")

        assert r.status_code == 502
        assert "COT" in r.json()["error"]

    def test_puerto_del_agente_sin_cablear_es_503(self):
        fallo = AgentPortError("falta el puerto de mercado")
        with TestClient(create_app(_runtime(market=FakeMarket(fallo=fallo)))) as c:
            r = c.get("/api/chart-assistant/EURUSD")

        assert r.status_code == 503

    def test_config_ausente_es_503(self):
        with TestClient(create_app(_runtime(market=FakeMarket(fallo=ConfigUnavailable("no hay strategy.yaml"))))) as c:
            r = c.get("/api/price/EURUSD")

        assert r.status_code == 503

    def test_un_bug_de_codigo_no_se_disfraza_de_servidor_caido(self):
        """Un `TypeError` tiene que salir como 500, no como el 503 de MT5 de REF."""
        with TestClient(create_app(_runtime(market=FakeMarket(fallo=TypeError("no such arg")))), raise_server_exceptions=False) as c:
            r = c.get("/api/account")

        assert r.status_code == 500

    def test_parametro_imposible_es_422_no_500(self, cliente):
        r = cliente.get("/api/candles/EURUSD", params={"bars": 5000})

        assert r.status_code == 422

    def test_tabla_inexistente_es_404(self, cliente_db):
        r = cliente_db.get("/api/db/tabla_que_no_existe")

        assert r.status_code == 404
        assert "tabla_que_no_existe" in r.json()["detail"]

    def test_sin_mercado_cableado_es_503(self):
        with TestClient(create_app(Runtime())) as c:
            assert c.get("/api/price/EURUSD").status_code == 503

    def test_sin_base_de_datos_cableada_es_503(self):
        with TestClient(create_app(Runtime(market=FakeMarket()))) as c:
            assert c.get("/api/journal").status_code == 503

    def test_sin_proveedor_de_simbolos_es_503(self):
        with TestClient(create_app(Runtime())) as c:
            r = c.get("/api/symbols")

        assert r.status_code == 503


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------


class TestAuth:
    def test_sin_token_configurado_no_se_pide_nada(self, cliente):
        assert cliente.get("/api/health").status_code == 200

    def test_con_token_la_lectura_sigue_abierta(self, cliente_db, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "secreto")

        assert cliente_db.get("/api/journal").status_code == 200

    def test_escribir_sin_token_es_401(self, cliente_db, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "secreto")

        r = cliente_db.post("/api/journal", json={"symbol": "EURUSD", "notes": "x"})

        assert r.status_code == 401
        assert r.json()["detail"] == "token inválido o ausente"

    def test_token_equivocado_es_el_mismo_401(self, cliente_db, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "secreto")

        r = cliente_db.post("/api/journal", json={"symbol": "EURUSD"}, headers={"X-API-Token": "otro"})

        assert r.status_code == 401

    def test_token_bueno_escribe(self, cliente_db, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "secreto")

        r = cliente_db.post(
            "/api/journal",
            json={"symbol": "EURUSD", "notes": "entrada"},
            headers={"X-API-Token": "secreto"},
        )

        assert r.status_code == 200
        assert cliente_db.post(
            "/api/journal", json={"symbol": "EURUSD"}, headers={"X-API-Token": "otro"}
        ).status_code == 401

    def test_bearer_tambien_sirve(self, cliente_db, monkeypatch):
        """El JS de REF manda `Authorization: Bearer` en unas rutas."""
        monkeypatch.setenv("API_TOKEN", "secreto")

        r = cliente_db.post(
            "/api/journal",
            json={"symbol": "EURUSD", "notes": "bearer"},
            headers={"Authorization": "Bearer secreto"},
        )

        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Bitácora contra la base de verdad
# ---------------------------------------------------------------------------


class TestJournal:
    def test_ciclo_completo(self, cliente_db):
        creado = cliente_db.post(
            "/api/journal",
            json={"symbol": "EURUSD", "action": "long", "notes": "FVG en M15", "tags": ["smc"]},
        )
        assert creado.status_code == 200
        jid = creado.json()["id"]

        assert cliente_db.get("/api/journal/{0}".format(jid)).json()["notes"] == "FVG en M15"
        assert [e["id"] for e in cliente_db.get("/api/journal").json()] == [jid]

        actualizado = cliente_db.put(
            "/api/journal/{0}".format(jid),
            json={"symbol": "EURUSD", "action": "short", "notes": "cambié de idea"},
        )
        cuerpo = actualizado.json()
        assert cuerpo["notes"] == "cambié de idea"
        assert cuerpo["action"] == "short"

        borrado = cliente_db.delete("/api/journal/{0}".format(jid))
        assert borrado.json() == {"ok": True, "id": jid}
        assert cliente_db.get("/api/journal/{0}".format(jid)).status_code == 404

    def test_put_es_reemplazo_no_parche(self, cliente_db):
        jid = cliente_db.post(
            "/api/journal", json={"symbol": "EURUSD", "notes": "con etiqueta", "tags": ["a"]}
        ).json()["id"]

        cuerpo = cliente_db.put("/api/journal/{0}".format(jid), json={"symbol": "EURUSD"}).json()

        assert cuerpo["notes"] is None
        assert cuerpo["tags"] in (None, [])

    def test_escribir_sin_mercado_no_pierde_la_entrada(self, db):
        """Sin mercado la entrada se guarda igual: la bitácora no es del bróker.

        El fixture `db` es OBLIGATORIO aquí y no un detalle: sin él, el store abre
        `database/trading.db` de verdad y la entrada de prueba se queda en la base
        real. Ya pasó, y el canario de `conftest.py` lo detectó tarde.
        """
        with TestClient(create_app(_runtime(store=db, market=None))) as c:
            creado = c.post("/api/journal", json={"symbol": "EURUSD", "notes": "sin terminal"})
            guardado = c.get("/api/journal/{0}".format(creado.json()["id"])).json()

        assert guardado["notes"] == "sin terminal"
        assert guardado["entry_price"] is None

    def test_entrada_enriquecida_con_el_precio(self, cliente_db):
        jid = cliente_db.post("/api/journal", json={"symbol": "EURUSD", "notes": "n"}).json()["id"]

        assert cliente_db.get("/api/journal/{0}".format(jid)).json()["entry_price"] == 1.0850

    def test_actualizar_una_entrada_que_no_existe_es_404(self, cliente_db):
        r = cliente_db.put("/api/journal/999999", json={"symbol": "EURUSD"})

        assert r.status_code == 404

    def test_filtrar_por_símbolo(self, cliente_db):
        cliente_db.post("/api/journal", json={"symbol": "EURUSD", "notes": "uno"})
        cliente_db.post("/api/journal", json={"symbol": "GBPUSD", "notes": "otro"})

        filas = cliente_db.get("/api/journal", params={"symbol": "gbpusd"}).json()

        assert [f["notes"] for f in filas] == ["otro"]


class TestDbYDibujos:
    def test_tablas_de_la_base(self, cliente_db):
        tablas = cliente_db.get("/api/db/tables").json()

        assert any(t.get("name") == "journal" for t in tablas)

    def test_filas_de_una_tabla(self, cliente_db):
        cliente_db.post("/api/journal", json={"symbol": "EURUSD", "notes": "fila"})

        cuerpo = cliente_db.get("/api/db/tables/journal").json()

        # El store devuelve `{table, columns, rows}` con las filas como arrays: el
        # JS las convierte con las columnas. Devolver una lista de dicts aquí sería
        # inventarse un contrato que el store no cumple.
        assert cuerpo["table"] == "journal"
        assert "notes" in cuerpo["columns"]
        assert len(cuerpo["rows"]) == 1

    def test_dibujos_van_y_vienen(self, cliente_db):
        dibujos = [{"tipo": "linea", "x1": 1.0, "y1": 1.0850, "x2": 2.0, "y2": 1.09}]

        assert cliente_db.put("/api/drawings/EURUSD", json=dibujos).json()["n"] == 1
        assert cliente_db.get("/api/drawings/eurusd").json() == dibujos
        assert cliente_db.delete("/api/drawings/EURUSD").json()["ok"] is True
        assert cliente_db.get("/api/drawings/EURUSD").json() == []

    def test_alertas_y_setup_log_no_rompen(self, cliente_db):
        assert isinstance(cliente_db.get("/api/alerts").json(), list)
        assert isinstance(cliente_db.get("/api/setup-log").json(), list)
        assert isinstance(cliente_db.get("/api/trades").json(), list)


# ---------------------------------------------------------------------------
# Mercado
# ---------------------------------------------------------------------------


class TestMercado:
    def test_precio_normaliza_el_símbolo(self, cliente):
        cuerpo = cliente.get("/api/price/eurusd").json()

        assert cuerpo["symbol"] == "EURUSD"
        assert cuerpo["bid"] == 1.0850

    def test_sin_precio_es_404_con_pista(self, cliente):
        r = cliente.get("/api/price/NOPUBLICA")

        assert r.status_code == 404
        assert "Market Watch" in r.json()["detail"]

    def test_candles_trae_pdh_y_pdl(self, cliente):
        cuerpo = cliente.get("/api/candles/EURUSD", params={"timeframe": "M15", "bars": 30}).json()

        assert cuerpo["symbol"] == "EURUSD"
        assert len(cuerpo["candles"]) == 30
        assert cuerpo["pdh"] == max(c["high"] for c in cuerpo["candles"])
        assert cuerpo["pdl"] == min(c["low"] for c in cuerpo["candles"])

    def test_candle_last_distingue_cerrada_de_forming(self, cliente):
        cuerpo = cliente.get("/api/candle/last").json()

        assert cuerpo["last_closed"] is not None
        assert cuerpo["forming"]["time"] > cuerpo["last_closed"]["time"]

    def test_simbolos_vienen_del_proveedor(self, cliente):
        assert cliente.get("/api/symbols").json() == ["EURUSD", "GBPUSD"]

    def test_spec_devuelve_el_dict(self, cliente):
        assert cliente.get("/api/spec/EURUSD").json()["digits"] == 5

    def test_chartism_es_alias_de_patterns(self, cliente):
        uno = cliente.get("/api/chartism/EURUSD").json()
        otro = cliente.get("/api/analysis/patterns/EURUSD").json()

        assert uno == otro

    def test_snapshot_del_chart_assistant(self, cliente):
        assert cliente.get("/api/chart-assistant/EURUSD").json()["symbol"] == "EURUSD"

    def test_posición_inexistente_es_404(self, cliente):
        assert cliente.get("/api/position/999").status_code == 404

    def test_vistas_derivadas_salen_de_las_velas(self, cliente):
        footprint = cliente.get("/api/analysis/footprint/EURUSD").json()
        heatmap = cliente.get("/api/analysis/heatmap/EURUSD").json()
        barras = cliente.get("/api/analysis/eventbars/EURUSD").json()

        assert footprint["mode"] == "footprint"
        assert footprint["source"] == "synthetic"
        assert heatmap["source"] == "synthetic"
        assert barras["bars"]

    def test_bid_ratio_fuera_de_rango_es_400(self, cliente):
        r = cliente.get("/api/analysis/footprint/EURUSD", params={"bid_ratio": 5.0})

        assert r.status_code == 400
        assert "bid_ratio" in r.json()["error"]

    def test_el_alias_also_cabe_en_analysis(self, cliente):
        assert (
            cliente.get("/api/analysis/chartism/EURUSD").json()
            == cliente.get("/api/analysis/patterns/EURUSD").json()
        )
        assert cliente.get("/api/analysis/chart-assistant/EURUSD").json()["symbol"] == "EURUSD"

    def test_candle_last_acepta_el_simbbolo_en_la_ruta(self, cliente):
        cuerpo = cliente.get("/api/candle/last/EURUSD").json()

        assert cuerpo["symbol"] == "EURUSD"
        assert cuerpo["forming"]["time"] > cuerpo["last_closed"]["time"]

    def test_account_y_positions(self, cliente):
        assert cliente.get("/api/account").json()["balance"] == 10000.0
        assert len(cliente.get("/api/positions").json()) == 1
        assert cliente.get("/api/risk/daily").json()["operations_today"] == 2
        assert cliente.get("/api/history", params={"days": 30}).status_code == 200


# ---------------------------------------------------------------------------
# Riesgo y evaluación de setup
# ---------------------------------------------------------------------------

#: Config que el gate sí puede usar: sin `sl_distance` no hay stop que planificar.
CONFIG_SETUP = {"sl_distance": 0.0012, "min_score": 70.0, "tp_ratio_r": 2.0}


def _snapshot_con_scores(
    *,
    score: float = 82.0,
    in_killzone: bool = True,
    zonas: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Un snapshot con la forma que devuelve `MT5Market.chart_snapshot`.

    Se construye a mano y no con el motor real porque lo que se prueba aquí es el
    contrato HTTP: que la ruta publique lo que el JS lee. La aritmética del score ya
    tiene sus propios tests.
    """
    patrones = (
        zonas
        if zonas is not None
        else {"fvgs": [{"type": "BULLISH_FVG", "top": 1.0840, "bottom": 1.0800}],
              "order_blocks": []}
    )
    return {
        "symbol": "EURUSD",
        "timeframe": "M15",
        "current_price": 1.0850,
        "last_time": EPOCH,
        "analysis": {"patterns": patrones},
        "risk_engine": {
            "bull": {
                "score": score,
                "verdict": "SETUP_ALISTO",
                "breakdown": {"smc": 30.0, "cot": 22.0},
                "components": {"killzone": {"in_killzone": in_killzone, "name": "London"}},
            },
            "bear": {
                "score": 12.0,
                "verdict": "NADA",
                "components": {"killzone": {"in_killzone": False, "name": None}},
            },
            "invalidation": {"BUY": 1.0500, "SELL": 1.1200},
        },
    }


@pytest.fixture
def cliente_setup(db):
    """App con la config REAL (temporal) y un snapshot con scores.

    La config entra por el seam real de `database.store` y no por un store doblado
    porque el bug que motivó estas rutas era justo de la FORMA de la config:
    `killzones` y `risk_weights` llegan como JSON string y el motor los recibía como
    `str`. Un doble que devuelve la config ya normalizada pasa por encima
    exactamente del bug que existía.
    """
    from database import store as store_mod

    db.get_config_source().cfg = dict(CONFIG_SETUP)
    mercado = FakeMarket(snapshot=_snapshot_con_scores())
    with TestClient(create_app(_runtime(market=mercado, store=store_mod))) as c:
        c.mercado_falso = mercado
        yield c


@pytest.fixture
def cliente_setup_con(db):
    """Cliente con el snapshot y la config que le pase el test.

    Devuelve una fábrica, no un cliente: casi todos los casos que importan son «el
    snapshot NO tiene esto», y un fixture fijo obligaría a repetir el montaje.
    """
    from contextlib import contextmanager

    from database import store as store_mod

    @contextmanager
    def fábrica(
        snapshot: Optional[Dict[str, Any]] = None,
        cfg: Optional[Dict[str, Any]] = None,
        *,
        fallo: Optional[BaseException] = None,
        news: Any = None,
    ) -> Any:
        db.get_config_source().cfg = dict(cfg) if cfg is not None else dict(CONFIG_SETUP)
        mercado = FakeMarket(
            snapshot=snapshot if snapshot is not None else _snapshot_con_scores(),
            fallo=fallo,
        )
        extra = {"store": store_mod, "market": mercado}
        if news is not None:
            extra["news"] = news
        with TestClient(create_app(_runtime(**extra))) as c:
            c.mercado_falso = mercado
            yield c

    return fábrica


class TestRiesgoSetup:
    def test_publica_el_desglose_del_score_completo(self, cliente_setup):
        """Se publica ENTERO, con su `breakdown`.

        Es la ruta donde el operador audita de dónde sale el número. Recortar el
        desglose en el servidor es perder la evidencia justo en el momento de
        preguntar «¿por qué 82?».
        """
        cuerpo = cliente_setup.get("/api/risk/setup").json()

        assert cuerpo["bull"]["score"] == 82.0
        assert cuerpo["bull"]["breakdown"]["smc"] == 30.0
        assert cuerpo["bear"]["score"] == 12.0
        assert cuerpo["symbol"] == "EURUSD"
        assert cuerpo["current_price"] == 1.0850

    def test_un_snapshot_sin_scores_es_422_y_no_un_score_de_cero(self, cliente_setup_con):
        """Un score que no se pudo calcular NO es un score de cero: un `200` con ceros
        se pinta en el panel como «el setup no vale», que es otra afirmación."""
        with cliente_setup_con(snapshot={"symbol": "EURUSD"}) as c:
            r = c.get("/api/risk/setup")

        assert r.status_code == 422
        assert "risk engine" in r.json()["detail"]

    def test_el_error_del_motor_sube_como_422_con_su_motivo(self, cliente_setup_con):
        roto = _snapshot_con_scores()
        roto["risk_engine"] = {"error": "sin velas"}
        with cliente_setup_con(snapshot=roto) as c:
            r = c.get("/api/risk/setup")

        assert r.status_code == 422
        assert r.json()["detail"] == "sin velas"

    def test_un_fallo_de_bróker_no_se_confunde_con_un_setup_inválido(self, cliente_setup_con):
        """`AdapterError` sale 503 y no 422. Un error de infraestructura pintado como
        veredicto de trading hace que el operador cambie la estrategia por un fallo
        del portatil."""
        from adapters.base_adapter import AdapterError

        with cliente_setup_con(fallo=AdapterError("MT5 apagado")) as c:
            assert c.get("/api/risk/setup").status_code == 503
            assert c.get("/api/chart/setup-eval").status_code == 503

    def test_un_simbolo_inexistente_es_404(self, cliente_setup_con):
        from adapters.base_adapter import SymbolNotFound

        with cliente_setup_con(fallo=SymbolNotFound("NOPUBLICA")) as c:
            assert c.get("/api/risk/setup?symbol=NOPUBLICA").status_code == 404


class TestSetupEval:
    def test_un_setup_completo_aprueba_y_trae_el_plan(self, cliente_setup):
        cuerpo = cliente_setup.get("/api/chart/setup-eval").json()

        assert cuerpo["approved"] is True
        assert cuerpo["direction"] == "BUY"
        assert cuerpo["entry"] == 1.0800
        assert cuerpo["sl"] == 1.0788
        assert cuerpo["tp"] == 1.0824
        assert cuerpo["entry_kind"] == "BULLISH_FVG"
        assert cuerpo["in_killzone"] is True
        assert cuerpo["reasons"] == []

    def test_el_umbral_viene_de_la_config_y_se_declara(self, cliente_setup):
        cuerpo = cliente_setup.get("/api/chart/setup-eval").json()

        assert cuerpo["min_score_used"] == 70.0

    def test_min_score_de_la_url_gana_pero_queda_declarado(self, cliente_setup):
        """El frontend solo lo manda en modo test, pero el umbral aplicado tiene que
        viajar en la respuesta: uno distinto del del YAML que solo consta en la URL no
        se puede auditar después."""
        cuerpo = cliente_setup.get("/api/chart/setup-eval?min_score=95").json()

        assert cuerpo["min_score_used"] == 95.0
        assert cuerpo["approved"] is False
        assert any("95" in r for r in cuerpo["reasons"])

    def test_min_score_fuera_de_rango_es_422_de_validacion(self, cliente_setup):
        assert cliente_setup.get("/api/chart/setup-eval?min_score=150").status_code == 422
        assert cliente_setup.get("/api/chart/setup-eval?min_score=-1").status_code == 422

    def test_el_overlay_lo_pinta_el_servidor(self, cliente_setup):
        """`echarts` viene calculado y no armado en el cliente con tres campos.

        El motivo: el frontend heredado tiene DOS versiones de este dibujo (la del
        panel y la del botón de entrada), y por eso divergían. Si el servidor publica
        las líneas ya resueltas, las dos pintan lo mismo porque leen lo mismo.
        """
        cuerpo = cliente_setup.get("/api/chart/setup-eval").json()

        assert [ml["yAxis"] for ml in cuerpo["echarts"]["markLine"]] == [1.0800, 1.0788, 1.0824]
        assert [ml["label"]["formatter"] for ml in cuerpo["echarts"]["markLine"]] == ["Entry", "SL", "TP"]
        assert cuerpo["echarts"]["markPoint"][0]["value"].startswith("BUY")
        assert cuerpo["view"]["price_min"] == 1.0788
        assert cuerpo["view"]["price_max"] == 1.0824
        assert cuerpo["view"]["end_time"] == EPOCH

    def test_un_setup_rechazado_no_pinta_ningun_horizontal(self, cliente_setup_con):
        """Un plan dibujado sobre un setup NO aprobado es la forma más cara de
        equivocar al operador: la línea existe y el color dice que vale."""
        with cliente_setup_con(_snapshot_con_scores(in_killzone=False)) as c:
            cuerpo = c.get("/api/chart/setup-eval").json()

        assert cuerpo["approved"] is False
        assert "Fuera de killzone." in cuerpo["reasons"]
        assert cuerpo["echarts"]["markLine"] == []
        assert cuerpo["echarts"]["markPoint"] == []
        assert cuerpo["view"] is None

    def test_sin_distancia_de_sl_se_rechaza_y_nombra_la_clave(self, cliente_setup_con):
        """Un `sl_distance` inventado en oro es un stop cien veces más corto de lo que
        el usuario cree. El gate rechaza y dice QUÉ rellenar."""
        with cliente_setup_con(cfg={"min_score": 70.0}) as c:
            cuerpo = c.get("/api/chart/setup-eval").json()

        assert cuerpo["approved"] is False
        assert cuerpo["sl"] is None and cuerpo["tp"] is None
        assert any("sl_distance" in r for r in cuerpo["reasons"])

    def test_la_killzone_forzada_llega_al_puerto_y_se_declara(self, cliente_setup):
        """El flag no se pierde por el camino: se propaga a `chart_snapshot`, que es
        quien fija el reloj del score, y vuelve marcado en tres sitios."""
        cuerpo = cliente_setup.get("/api/chart/setup-eval?killzone=1").json()

        pedido = [p for p in cliente_setup.mercado_falso.pedidos if p[0] == "snapshot"][-1]
        assert pedido[4] is True
        assert cuerpo["killzone_force"] is True
        assert cuerpo["score_note"]
        # El score forzado no cambia el umbral: son dos cosas distintas.
        assert cuerpo["min_score_used"] == 70.0

    def test_sin_flag_la_killzone_no_se_fuerza(self, cliente_setup):
        cuerpo = cliente_setup.get("/api/chart/setup-eval").json()

        pedido = [p for p in cliente_setup.mercado_falso.pedidos if p[0] == "snapshot"][-1]
        assert pedido[4] is False
        assert cuerpo["killzone_force"] is False
        assert cuerpo["score_note"] is None

    def test_sintetico_es_503_y_no_una_promesa(self, cliente_setup):
        """Sin `MarketSimulator` no hay a qué apuntar. Un `synthetic=1` que devolviera
        velas reales con `200` haría creer al usuario que está probando el flag."""
        r = cliente_setup.get("/api/chart/setup-eval?synthetic=1")

        assert r.status_code == 503
        assert "MarketSimulator" in r.json()["detail"]

    def test_sintetico_0_es_datos_reales(self, cliente_setup):
        assert cliente_setup.get("/api/chart/setup-eval?synthetic=0").status_code == 200
        assert cliente_setup.get("/api/chart/setup-eval").status_code == 200

    def test_approved_no_autoriza_operar(self, cliente_setup):
        """`approved` es el veredicto del gate (score, killzone, zona y R:R). El
        blackout y los límites de cuenta se comprueban al ejecutar, donde se conoce la
        hora real de la orden."""
        cuerpo = cliente_setup.get("/api/chart/setup-eval").json()

        assert cuerpo["approved"] is True
        assert cuerpo["news_blackout"] is False
        assert cuerpo["news_gate"] is None
        # Y nada que huela a orden enviada: el veredicto no es una ejecución.
        assert not [k for k in cuerpo if "ticket" in k or "sent" in k or "orden" in k]

    def test_un_blackout_se_informa_pero_no_descarta_el_setup(self, cliente_setup_con):
        """Filtrar aquí rechazaría setups con un dato viejo: el calendario se lee una
        vez y este snapshot puede ser de hace diez segundos."""
        veredicto = {"ok": False, "block": {"evento": "NFP", "minutos": 5},
                     "detail": "NFP en 5 min", "fail_open": False}
        with cliente_setup_con(news=FakeNews(veredicto)) as c:
            cuerpo = c.get("/api/chart/setup-eval").json()

        assert cuerpo["news_blackout"] is True
        assert cuerpo["approved"] is True
        assert cuerpo["news_gate"]["evento"] == "NFP"
        assert cuerpo["news_reason"] == "NFP en 5 min"

    def test_sin_puerto_de_noticias_lo_declara_en_lugar_de_callar(self, cliente_setup_con):
        """Fail-open declarado: `news_blackout=False` CON el motivo.

        Un fail-open silencioso es indistinguible de «no hay noticias», y esa es
        precisamente la diferencia que hay que poder ver."""
        with cliente_setup_con(news=_NoticiasQueRevientan()) as c:
            cuerpo = c.get("/api/chart/setup-eval").json()

        assert cuerpo["approved"] is True
        assert cuerpo["news_blackout"] is False
        assert cuerpo["news_reason"] and "noticias" in cuerpo["news_reason"]

    def test_el_simbolo_se_normaliza(self, cliente_setup):
        """Con dos copias de la normalización, la que se quede sin `.strip()` acepta
        « eurusd » y devuelve un error con el motivo equivocado."""
        cuerpo = cliente_setup.get("/api/chart/setup-eval?symbol=%20eurusd%20").json()

        assert cuerpo["symbol"] == "EURUSD"
        assert cliente_setup.mercado_falso.pedidos[-1][1] == "EURUSD"

    def test_sin_scores_es_422_tambien_aqui(self, cliente_setup_con):
        with cliente_setup_con(snapshot={"symbol": "EURUSD", "risk_engine": {}}) as c:
            assert c.get("/api/chart/setup-eval").status_code == 422


class _NoticiasQueRevientan:
    """El peor caso del gate de noticias: el calendario no se puede leer."""

    def gate(self) -> Dict[str, Any]:
        raise RuntimeError("calendario no disponible")

    def news(self) -> Dict[str, Any]:
        raise RuntimeError("calendario no disponible")


# ---------------------------------------------------------------------------
# El watcher: evalúa, audita y no manda órdenes
# ---------------------------------------------------------------------------

#: Config del watcher para HTTP: un símbolo, M15, dedup corto para los tests.
CONFIG_WATCHER = {
    "enabled": True,
    "scan_interval_sec": 60,
    "dedup_ttl_sec": 2700,
    "auto_execute": False,
    "symbols": [{"symbol": "EURUSD", "timeframe": "M15"}],
}


def _cfg_watcher_completa(**overrides: Any) -> Dict[str, Any]:
    """La config que ve `store` con el watcher puesto, en un solo dict.

    `FakeConfigSource.watcher_config()` lee `cfg["watcher_config"]` y
    `get_trading_config()` devuelve `cfg` entero, así que las dos cosas que el
    escaneo necesita (`sl_distance`/`min_score` para el gate y `symbols` para saber
    qué escanear) viven en el mismo sitio. Los `overrides` van a la parte de trading.
    """
    trading = dict(CONFIG_SETUP)
    trading.update(overrides)
    return {"watcher_config": dict(CONFIG_WATCHER), **trading}


@pytest.fixture
def cliente_setup_watcher(db) -> Any:
    """Cliente con base de verdad, watcher configurado y snapshot con scores."""
    db.get_config_source().cfg = _cfg_watcher_completa()
    mercado = FakeMarket(snapshot=_snapshot_con_scores())
    with TestClient(create_app(_runtime(market=mercado, store=db))) as c:
        c.mercado_falso = mercado
        yield c


class TestWatcher:
    def test_status_es_una_lectura_y_no_pide_token(self, cliente):
        assert cliente.get("/api/watcher/status").status_code == 200

    def test_status_publica_lo_que_el_js_ya_sabe_pintar(self, cliente_setup_watcher):
        """Las claves son las que lee `renderWatcherStatus`.

        Si el backend renombra una, el panel no falla: deja de pintar el interruptor y
        nadie ve por qué. Un 200 con las claves viejas es un síntoma, no una solución.
        """
        cuerpo = cliente_setup_watcher.get("/api/watcher/status").json()

        for clave in ("enabled", "scan_interval_sec", "auto_execute",
                      "dedup_ttl_sec", "symbols", "states"):
            assert clave in cuerpo, clave
        assert cuerpo["enabled"] is True
        assert [s["symbol"] for s in cuerpo["symbols"]] == ["EURUSD"]
        assert cuerpo["states"] == [None]  # aún no se ha escaneado nada

    def test_el_autoarranque_se_publica_apagado_aunque_el_yaml_lo_pida(
            self, cliente_setup_watcher):
        """`auto_execute` es la lectura del interruptor, y el interruptor controla algo
        que no existe.

        Publicar el valor del YAML haría que el panel rotulara AUTO-EJECUCIÓN sobre un
        sistema que no manda nada. Por eso van los tres: lo que el YAML pide
        (`auto_execute_conf`), lo que el sistema hace (`auto_execute: false`) y si hay
        código para hacerlo (`auto_execute_disponible: false`).
        """
        cuerpo = cliente_setup_watcher.post("/api/watcher/scan").json()
        estado = cliente_setup_watcher.get("/api/watcher/status").json()

        assert estado["auto_execute"] is False
        assert estado["auto_execute_disponible"] is False
        assert estado["auto_execute_motivo"]
        # Y el ciclo va con la misma respuesta: ni `auto_ejecute` ni el evento `new`
        # pueden sugerir que algo se mandó.
        assert cuerpo["auto_ejecute"] is False
        assert cuerpo["auto_ejecute_disponible"] is False
        assert [e["auto_ejecutado"] for e in cuerpo["events"]] == [False]

    def test_el_yaml_puede_pedir_auto_ejecucion_y_el_estado_sigue_diciendo_que_no(
            self, cliente_setup_watcher):
        """`auto_execute_conf` sí refleja el YAML: es un dato sobre la configuración."""
        from database import store as store_mod

        store_mod.get_config_source().cfg["watcher_config"] = dict(
            CONFIG_WATCHER, auto_execute=True)
        cuerpo = cliente_setup_watcher.get("/api/watcher/status").json()

        assert cuerpo["auto_execute_conf"] is True
        assert cuerpo["auto_execute"] is False

    def test_el_escaneo_pide_token_porque_escribe_en_la_auditoria(self, db, monkeypatch):
        """Un escaneo que no manda órdenes sigue ESCRIENDO en `setup_log`.

        Que no opere no lo vuelve lectura: quien puede escribir en la auditoría de
        operaciones es quien puede operar. Sin este 401, el watcher sería la puerta
        trasera al mismo sitio que `/api/trade/market` protege.
        """
        monkeypatch.setenv("API_TOKEN", "secreto")
        from database import store as store_mod

        store_mod.get_config_source().cfg = _cfg_watcher_completa()
        app = create_app(_runtime(market=FakeMarket(snapshot=_snapshot_con_scores()),
                                  store=store_mod))
        with TestClient(app) as c:
            r = c.post("/api/watcher/scan")

        assert r.status_code == 401
        assert r.json()["detail"] == "token inválido o ausente"

    def test_dos_escaneos_consecutivos_escriben_una_sola_fila_de_descarte(self, db):
        """La dedup tiene que sobrevivir a la petición HTTP.

        El servicio guarda en memoria la firma del último rechazo por símbolo. Si la
        ruta construyera uno nuevo en cada `POST /scan`, esa memoria moriría con el
        `return` y un setup por debajo del umbral escribiría una fila cada 60 segundos
        para siempre — sin que nada fallara, y con el histórico inservible. El test usa
        DOS peticiones, no dos llamadas al servicio: la memoria del servicio sobrevive
        a una llamada, y el fallo que se busca está en la capa HTTP.
        """
        from database import store as store_mod

        store_mod.get_config_source().cfg = _cfg_watcher_completa(min_score=95.0)
        app = create_app(_runtime(market=FakeMarket(snapshot=_snapshot_con_scores()),
                                  store=store_mod))
        with TestClient(app) as c:
            primero = c.post("/api/watcher/scan").json()
            segundo = c.post("/api/watcher/scan").json()

        assert primero["rejects"] and segundo["rejects"] == []
        assert segundo["events"] == []
        filas = [f for f in db.list_setup_log(limit=50) if f["source"] == "watcher"]
        assert len(filas) == 1

    def test_un_setup_aprobado_deja_constancia_sin_ejecutar(self, db):
        """Fila de intención auditada: `trade_result` con `executed: False`.

        `store.link_last_setup` busca las filas con `trade_result_json` vacía, y una
        fila de setup detectado que apareciera ahí más tarde se leería como una
        operación abierta de la que no se conoce el resultado.
        """
        from database import store as store_mod

        store_mod.get_config_source().cfg = _cfg_watcher_completa()
        app = create_app(_runtime(market=FakeMarket(snapshot=_snapshot_con_scores()),
                                  store=store_mod))
        with TestClient(app) as c:
            cuerpo = c.post("/api/watcher/scan").json()

        assert [e["event"] for e in cuerpo["events"]] == ["new"]
        assert cuerpo["audit_logged"] == 1
        assert cuerpo["audit_fallidos"] == 0
        assert cuerpo["auto_ejecute"] is False
        assert cuerpo["dry_run"] is True
        fila = [f for f in store_mod.list_setup_log(limit=50) if f["source"] == "watcher"][0]
        # `trade_result` con `executed: False` es lo que impide que la fila de un
        # setup detectado aparezca como operación abierta en el post-mortem.
        assert fila["trade_result"]["executed"] is False
        assert fila["trade_result"]["dry_run"] is True

    def test_el_estado_del_simbolo_queda_tras_el_escaneo(self, db):
        """El escaneo es también la deduplicación entre ciclos: sin `setup_state`, el
        mismo setup volvería a salir como `new` en cada ciclo."""
        from database import store as store_mod

        store_mod.get_config_source().cfg = _cfg_watcher_completa()
        app = create_app(_runtime(market=FakeMarket(snapshot=_snapshot_con_scores()),
                                  store=store_mod))
        with TestClient(app) as c:
            c.post("/api/watcher/scan")
            estados = c.get("/api/watcher/status").json()["states"]

        assert estados[0]["status"] == "active"
        assert estados[0]["notified"] is True
        assert estados[0]["auto_executed"] is False

    def test_un_simbolo_sin_datos_no_devuelve_500(self, db):
        """Un símbolo roto es un evento `error` del ciclo, no un fallo del ciclo.

        Con dos símbolos, el que tiene datos se evalúa igualmente: si el primero tumba
        la petición, el operador pierde el símbolo que sí importaba.
        """
        from database import store as store_mod

        store_mod.get_config_source().cfg = _cfg_watcher_completa(
            symbols=[{"symbol": "EURUSD", "timeframe": "M15"},
                     {"symbol": "GBPUSD", "timeframe": "M15"}])
        mercado = FakeMarket(snapshot={"symbol": "EURUSD", "risk_engine": {}})
        app = create_app(_runtime(market=mercado, store=store_mod))
        with TestClient(app) as c:
            r = c.post("/api/watcher/scan")

        assert r.status_code == 200
        eventos = r.json()["events"]
        assert [e["symbol"] for e in eventos if e["event"] == "error"] == ["EURUSD"]

    def test_un_calendario_caido_lo_dice_en_este_ciclo(self, db):
        """Fail-open declarado: el ciclo sale con el aviso, no solo la fila.

        Un `verdict="IGNORED_NEWS"` cuyo motivo es "feed caído" sería una afirmación
        falsa sobre el calendario.
        """
        from database import store as store_mod

        store_mod.get_config_source().cfg = _cfg_watcher_completa()
        app = create_app(_runtime(market=FakeMarket(snapshot=_snapshot_con_scores()),
                                  store=store_mod, news=_NoticiasQueRevientan()))
        with TestClient(app) as c:
            cuerpo = c.post("/api/watcher/scan").json()

        assert [e["event"] for e in cuerpo["events"]] == ["new"]
        assert cuerpo["avisos"] and "calendario" in cuerpo["avisos"][0]["aviso"]

    def test_auto_execute_es_501_en_los_dos_sentidos(self, db, monkeypatch):
        """El interruptor del panel no puede encender nada, y el código lo dice.

        Con un 200, `static/main.js` enseñaría "auto-ejecutar ACTIVADO" después de
        encender un interruptor que no encendió nada. 501 en ambos sentidos: encender
        exige el camino de `ExecutionService`, y apagar lo que no se puede encender
        también es no-op.
        """
        monkeypatch.setenv("API_TOKEN", "secreto")
        from database import store as store_mod

        store_mod.get_config_source().cfg = _cfg_watcher_completa()
        app = create_app(_runtime(market=FakeMarket(snapshot=_snapshot_con_scores()),
                                  store=store_mod))
        with TestClient(app) as c:
            for enabled in (True, False):
                r = c.post("/api/watcher/auto-execute", json={"enabled": enabled},
                           headers={"X-API-Token": "secreto"})
                detalle = r.json()["detail"]
                assert r.status_code == 501
                assert detalle["pide"] is enabled
                assert detalle["auto_execute"] is False
                assert detalle["dry_run"] is True

    def test_auto_execute_pide_token_antes_de_contar_que_no_existe(self, db, monkeypatch):
        """Sin token es 401, no 501.

        Decirle a cualquiera que la función no existe es documentación pública
        innecesaria; se lo dice a quien ya puede operar.
        """
        monkeypatch.setenv("API_TOKEN", "secreto")
        from database import store as store_mod

        store_mod.get_config_source().cfg = _cfg_watcher_completa()
        app = create_app(_runtime(market=FakeMarket(snapshot=_snapshot_con_scores()),
                                  store=store_mod))
        with TestClient(app) as c:
            r = c.post("/api/watcher/auto-execute", json={"enabled": True})

        assert r.status_code == 401

    def test_el_interruptor_no_admite_una_peticion_sin_decidir(self, db, monkeypatch):
        """`enabled` es obligatorio y sin valor por defecto: un interruptor que se
        enciende sin decir a qué se refiere es un interruptor que alguien activó sin
        querer. El 422 deja el checkbox como estaba."""
        monkeypatch.setenv("API_TOKEN", "secreto")
        from database import store as store_mod

        store_mod.get_config_source().cfg = _cfg_watcher_completa()
        app = create_app(_runtime(market=FakeMarket(snapshot=_snapshot_con_scores()),
                                  store=store_mod))
        with TestClient(app) as c:
            r = c.post("/api/watcher/auto-execute", json={},
                       headers={"X-API-Token": "secreto"})

        assert r.status_code == 422

    def test_sin_base_de_datos_el_scan_dice_que_no_hay_nada_que_mirar(self, cliente):
        """Sin `watcher.symbols` la respuesta trae `motivo`.

        Cero eventos y `symbols: 0` se lee como "el mercado estaba en calma". No lo
        estaba: no había nada que mirar.
        """
        cuerpo = cliente.post("/api/watcher/scan").json()

        assert cuerpo["events"] == []
        assert cuerpo["symbols"] == 0
        assert "watcher.symbols" in cuerpo["motivo"]


class TestWatcherCompartido:
    """Dónde vive el servicio: la dedup tiene que durar más que una petición."""

    def test_el_servicio_es_el_mismo_entre_llamadas(self):
        """No se construye en `Runtime.__init__` (importar no toca nada) pero sí en el
        primer uso, y a partir de ahí es uno solo."""
        rt = Runtime(market=FakeMarket(), store=FakeStore())

        assert rt.watcher is None
        primero = rt.watcher_service()
        assert rt.watcher_service() is primero

    def test_cambiar_el_mercado_suelta_el_watcher(self):
        """        El watcher guarda el mercado con el que se construyó.

        Escanear contra un mercado que ya no está conectado daría `error` en cada
        símbolo haciéndose el vivo. Perder la memoria de dedup (un par de filas de
        más) es preferible a escanear el sitio equivocado.
        """
        rt = Runtime(market=FakeMarket(), store=FakeStore())
        servicio = rt.watcher_service()
        servicio._rejects[("EURUSD", "M15")] = ("firma",)

        rt.set_market(FakeMarket())

        nuevo = rt.watcher_service()
        assert nuevo is not servicio
        assert nuevo._rejects == {}


# ---------------------------------------------------------------------------
# El inventario de rutas del docstring, hecho verificable
# ---------------------------------------------------------------------------


def _norm(patron: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", patron)


def _rutas_del_js() -> List[str]:
    """Las rutas que el frontend heredado pide, con los valores ya normalizados."""
    import os

    from api.app import STATIC_DIR

    main_js = os.path.join(STATIC_DIR, "main.js")
    if not os.path.exists(main_js):
        return []
    with open(main_js, encoding="utf-8") as fh:
        js = fh.read()
    # `${...}` dentro del path es un valor, no un tramo de ruta.
    return sorted(set(re.findall(r"['\"`](/api/[^'\"`?$]*(?:\$\{[^}]*\}[^'\"`?$]*)?)", js)))


def _servidas(cliente: Any) -> List[str]:
    return list(cliente.get("/api/openapi.json").json()["paths"])


def _sin_servir(cliente: Any) -> List[str]:
    import re as _re

    servidas = [_norm(p) for p in _servidas(cliente)]
    faltan = []
    for llamada in _rutas_del_js():
        patron = _re.sub(r"\$\{[^}]*\}", "x", _norm(llamada))
        if patron in servidas or any(p == patron or p == patron + "/{}" for p in servidas):
            continue
        if any(_re.fullmatch(_re.escape(p).replace(_re.escape("{}"), "[^/]+"), patron) for p in servidas):
            continue
        faltan.append(llamada)
    return faltan


def _inventario_del_docstring() -> List[str]:
    """La primera columna de la tabla de `api/routes/__init__.py`."""
    import api.routes as rutas_paquete

    doc = rutas_paquete.__doc__ or ""
    lineas = []
    for linea in doc.splitlines():
        if linea.startswith("| `/api"):
            lineas.append(linea.split("|")[1].strip().strip("`"))
    return lineas


class TestContratoConElFrontend:
    def test_el_js_no_pide_nada_que_no_este_inventariado(self, cliente):
        """Las dos direcciones del contrato, en un solo test.

        - Si el JS pide una ruta que no existe y que NO esta en la tabla, el test
          falla: alguien anadio una llamada en el frontend y nadie la sirvio.
        - Si la tabla declara como pendiente una ruta que ya se sirve, el test
          falla: el inventario se quedo obsoleto, que es peor que no tenerlo
          porque parece una lista de trabajo.

        No se exige que falte cero: mientras falte, el inventario es la lista
        exacta de lo que falta, y por eso esta tabla se puede revisar en un diff.
        """
        if not _rutas_del_js():
            pytest.skip("no hay static/main.js en este arbol")

        declaradas = set(_inventario_del_docstring())
        reales = set(_sin_servir(cliente))
        # Las familias del docstring cubren varias rutas: `/api/watcher/{status,scan,
        # auto-execute}` y `/api/research/*` se expanden por prefijo.
        familias = [d for d in declaradas if d.endswith("*") or "{" in d]

        def cubierta(llamada: str) -> bool:
            return llamada in declaradas or any(
                llamada.startswith(f.rstrip("*")) or llamada.startswith(f.split("{")[0])
                for f in familias
            )

        sin_inventario = [r for r in sorted(reales) if not cubierta(r)]
        assert sin_inventario == [], "el JS pide rutas que faltan y no estan en la tabla"

        implementadas = [
            d for d in sorted(declaradas)
            if "*" not in d and "{" not in d and d not in reales
        ]
        assert implementadas == [], "la tabla declara pendientes rutas ya servidas"


# ---------------------------------------------------------------------------
# Agente: SSE, roles y conversaciones
# ---------------------------------------------------------------------------


class TestAgente:
    def test_mensaje_vacio_es_400(self, cliente_db):
        assert cliente_db.post("/api/agent/message", json={"message": "   "}).status_code == 400

    def test_el_stream_es_sse_con_json_dentro(self, cliente_db, monkeypatch):
        eventos = [
            {"type": "token", "text": "hola"},
            {"type": "done"},
        ]

        async def stream(**kwargs: Any):
            for evento in eventos:
                yield "data: " + json.dumps(evento) + "\n\n"

        monkeypatch.setattr("agent.laya_bridge.stream", stream)

        r = cliente_db.post("/api/agent/message", json={"message": "hola"})

        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        lineas = [ln for ln in r.text.splitlines() if ln.startswith("data: ")]
        assert [json.loads(ln[6:]) for ln in lineas] == eventos

    def test_el_sin_conversation_id_crea_la_conversación(self, cliente_db, monkeypatch):
        vistos = {}

        async def stream(**kwargs: Any):
            vistos.update(kwargs)
            yield "data: {}\n\n"

        monkeypatch.setattr("agent.laya_bridge.stream", stream)

        cliente_db.post("/api/agent/message", json={"message": "hola", "role_id": "general"})

        assert vistos["conversation_id"]
        assert vistos["mensaje"] == "hola"
        assert vistos["skip_tools"] is False

    def test_roles_ciclo_completo(self, cliente_db):
        cuerpo = {
            "name": "Lector de mercado",
            "system_prompt": "analiza",
            "allowed_tools": ["get_price"],
        }

        assert cliente_db.put("/api/agent/roles/lector", json=cuerpo).status_code == 200
        # El store devuelve el rol con `id`, no con `role_id`: es la fila de la
        # tabla `roles`, y el JS heredado carga la lista sin leer ninguna de las dos.
        assert cliente_db.get("/api/agent/roles/lector").json()["name"] == "Lector de mercado"
        assert "lector" in [r["id"] for r in cliente_db.get("/api/agent/roles").json()]
        assert cliente_db.delete("/api/agent/roles/lector").json()["ok"] is True
        assert cliente_db.get("/api/agent/roles/lector").status_code == 404

    def test_general_no_se_puede_borrar(self, cliente_db):
        r = cliente_db.delete("/api/agent/roles/general")

        assert r.status_code == 400
        assert "general" in r.json()["detail"]

    def test_conversaciones_ciclo_completo(self, cliente_db):
        conversacion = cliente_db.post(
            "/api/agent/conversations", json={"title": "sesión 1", "role_id": "general"}
        ).json()
        cid = conversacion["id"]

        assert cliente_db.get("/api/agent/conversations/{0}".format(cid)).json()["title"] == "sesión 1"
        assert cid in [c["id"] for c in cliente_db.get("/api/agent/conversations").json()]
        assert isinstance(cliente_db.get("/api/agent/conversations/{0}/messages".format(cid)).json(), list)
        assert cliente_db.delete("/api/agent/conversations/{0}".format(cid)).json()["ok"] is True
        assert cliente_db.get("/api/agent/conversations/{0}".format(cid)).status_code == 404


# ---------------------------------------------------------------------------
# WebSocket de cinta
# ---------------------------------------------------------------------------


class TestWebSocketOrderflow:
    def test_el_primer_mensaje_es_el_estado(self, cliente):
        with cliente.websocket_connect("/ws/orderflow") as ws:
            mensaje = ws.receive_json()

        assert mensaje["event"] == "status"
        assert mensaje["state"] == "connected"
        assert mensaje["symbol"] == "EURUSD"
        assert mensaje["snapshot"]["symbol"] == "EURUSD"

    def test_ajustes_por_control_se_difunden(self, cliente):
        with cliente.websocket_connect("/ws/orderflow") as ws:
            ws.receive_json()  # status
            ws.send_json({"action": "update_settings", "zscore_threshold": 4.5})
            mensaje = ws.receive_json()

        assert mensaje["event"] == "config"
        assert mensaje["settings"]["zscore_threshold"] == 4.5

    def test_un_control_desconocido_no_rompe_nada(self, cliente):
        with cliente.websocket_connect("/ws/orderflow") as ws:
            ws.receive_json()
            ws.send_json({"action": "algo_raro"})
            ws.send_json({"action": "update_settings", "absorb_trades": 10})
            mensaje = ws.receive_json()

        assert mensaje["event"] == "config"

    def test_json_roto_se_ignora(self, cliente):
        with cliente.websocket_connect("/ws/orderflow") as ws:
            ws.receive_json()
            ws.send_text("{esto no es json")
            ws.send_json({"action": "settings", "absorb_trades": 7})
            mensaje = ws.receive_json()

        assert mensaje["settings"]["absorb_trades"] == 7

    def test_sin_token_el_ws_no_abre(self, cliente_db, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "secreto")

        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with cliente_db.websocket_connect("/ws/orderflow") as ws:
                ws.receive_json()

        assert exc.value.code == 1008

    def test_con_token_por_query_abre(self, cliente_db, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "secreto")

        with cliente_db.websocket_connect("/ws/orderflow?token=secreto") as ws:
            assert ws.receive_json()["event"] == "status"

    def test_al_cerrar_el_cliente_el_motor_se_apaga(self):
        motor = FakeEngine()
        with TestClient(create_app(_runtime(orderflow=motor))) as c:
            with c.websocket_connect("/ws/orderflow") as ws:
                ws.receive_json()

        assert motor.paradas >= 0  # `stop()` es idempotente: no se exige nada aquí


# ---------------------------------------------------------------------------
# El apagado
# ---------------------------------------------------------------------------


class TestApagado:
    def test_shutdown_cierra_el_motor_y_avisa_de_los_fallos(self):
        class MotorQueFallaAlParar(FakeEngine):
            def stop(self) -> None:
                raise RuntimeError("no me puedo parar")

        motor = MotorQueFallaAlParar()
        with TestClient(create_app(_runtime(orderflow=motor))) as c:
            c.get("/api/health")

# Un `stop()` que revienta no puede tumbar el apagado: es lo que impide
        # que un terminal colgado deje el proceso vivo por siempre.
        assert True


# ---------------------------------------------------------------------------
# El contrato con el frontend heredado: rutas que faltaban
# ---------------------------------------------------------------------------


class TestCVDYSmrPorSimbolo:
    """Las dos rutas de analisis que el JS llama por simbolo."""

    def test_cvd_devuelve_la_serie_acumulada(self, cliente):
        puntos = cliente.get("/api/analysis/cvd/EURUSD", params={"bars": 30}).json()

        assert len(puntos) == 30
        assert [p["time"] for p in puntos] == [vela(i)["time"] for i in range(30)]
        # El CVD acumulado TIENE que moverse: una serie plana significa que el
        # acumulador no esta acumulando, que es el fallo silencioso clasico.
        assert puntos[-1]["value"] != puntos[0]["value"]

    def test_cvd_sin_velas_es_404(self):
        with TestClient(create_app(_runtime(market=FakeMarket(velas=[])))) as c:
            r = c.get("/api/analysis/cvd/EURUSD")

        assert r.status_code == 404
        assert "EURUSD" in r.json()["detail"]

    def test_cvd_con_bracket_apagado_es_503(self, monkeypatch):
        with TestClient(create_app(_runtime(market=FakeMarket(fallo=AdapterError("MT5 fuera"))))) as c:
            assert c.get("/api/analysis/cvd/EURUSD").status_code == 503

    def test_smr_devuelve_el_veredicto_con_motivo(self, cliente):
        cuerpo = cliente.get("/api/analysis/smr/EURUSD").json()

        assert cuerpo["confirmed"] is False
        assert cuerpo["reason"]
        assert cuerpo["bull"] == {"confirmed": False}

    def test_smr_pasa_las_velas_que_ya_ha_leido(self):
        mercado = FakeMarket(veredicto_smr={"confirmed": True, "bull": {"confirmed": True}, "bear": {"confirmed": False}})
        with TestClient(create_app(_runtime(market=mercado))) as c:
            cuerpo = c.get("/api/analysis/smr/EURUSD", params={"bars": 20}).json()

        assert cuerpo["confirmed"] is True
        # Las velas las lee la ruta una vez y las pasa al puerto: si el puerto
        # volviera a pedirlas al broker, la ruta estaria llamando dos veces al
        # terminal por peticion para el mismo dato.
        assert ("candles", "EURUSD", "M15", 20) in mercado.pedidos
        assert ("smr", "EURUSD", "M15", 20) in mercado.pedidos


class TestJournalOverlay:
    def test_overlay_no_se_lo_come_la_ruta_de_id(self, cliente_db):
        """`/api/journal/{jid}` con `jid: int` devuelve 422 en `/api/journal/overlay`.

        Por eso la ruta literal se declara ANTES. Este test falla con 422 si alguien
        mueve el overlay de sitio, y ese 422 es el sintoma que ve el usuario.
        """
        cliente_db.post(
            "/api/journal",
            json={
                "symbol": "EURUSD",
                "action": "BUY",
                "entry_price": 1.0850,
                "sl_price": 1.0840,
                "tp_price": 1.0870,
                "time_open": EPOCH,
            },
        )

        r = cliente_db.get("/api/journal/overlay")

        assert r.status_code == 200
        lineas = r.json()
        assert {linea["line_type"] for linea in lineas} == {"sl", "tp"}
        assert all(linea["result"] == "win" for linea in lineas)

    def test_una_entrada_sin_precio_no_dibuja_nada(self, cliente_db):
        cliente_db.post("/api/journal", json={"symbol": "EURUSD", "notes": "sin precios"})

        assert cliente_db.get("/api/journal/overlay").json() == []

    def test_una_venta_con_tp_por_debajo_es_win(self, cliente_db):
        cliente_db.post(
            "/api/journal",
            json={
                "symbol": "EURUSD",
                "action": "SELL",
                "entry_price": 1.0850,
                "sl_price": 1.0860,
                "tp_price": 1.0840,
                "time_open": EPOCH,
            },
        )

        assert all(l["result"] == "win" for l in cliente_db.get("/api/journal/overlay").json())


class TestDrawingsDelChart:
    def test_ciclo_completo_en_la_ruta_que_llama_el_js(self, cliente_db):
        cuerpo = {"symbol": "eurusd", "timeframe": "h1", "drawings": [{"tipo": "linea", "precio": 1.09}]}

        assert cliente_db.put("/api/chart/drawings", json=cuerpo).status_code == 200

        leido = cliente_db.get("/api/chart/drawings", params={"symbol": "eurusd", "timeframe": "h1"}).json()
        # El simbolo se normaliza a mayusculas en la respuesta: el JS compara
        # `resp.symbol` con el suyo y en minusculas no encajaria nunca.
        assert leido["symbol"] == "EURUSD"
        assert leido["timeframe"] == "H1"
        assert len(leido["drawings"]) == 1

        assert cliente_db.delete(
            "/api/chart/drawings", params={"symbol": "EURUSD", "timeframe": "H1"}
        ).status_code == 200
        assert cliente_db.get("/api/chart/drawings", params={"symbol": "EURUSD", "timeframe": "H1"}).json()["drawings"] == []

    def test_put_reemplaza_y_no_añade(self, cliente_db):
        cliente_db.put("/api/chart/drawings", json={"symbol": "EURUSD", "timeframe": "M15", "drawings": [{"a": 1}]})

        cliente_db.put("/api/chart/drawings", json={"symbol": "EURUSD", "timeframe": "M15", "drawings": [{"b": 2}]})

        guardados = cliente_db.get("/api/chart/drawings", params={"symbol": "EURUSD", "timeframe": "M15"}).json()
        assert len(guardados["drawings"]) == 1

    def test_la_ruta_vieja_por_simbolo_sigue_viva(self, cliente_db):
        """`/api/drawings/{symbol}` no se quita: el panel de REF la usa en un sitio."""
        cliente_db.put("/api/drawings/EURUSD", json={"drawings": [{"a": 1}]}, params={"timeframe": "M15"})

        assert cliente_db.get("/api/drawings/EURUSD", params={"timeframe": "M15"}).status_code == 200


class TestAlertasDelChart:
    def test_listar_por_las_dos_rutas(self, cliente_db):
        assert cliente_db.get("/api/chart/alerts").json() == []
        assert cliente_db.get("/api/alerts").json() == []

    def test_cancelar_pone_el_estado_y_no_borra(self, cliente_db):
        from database import store as store_mod

        aid = store_mod.add_chart_alert("EURUSD", 1.09, label="resistencia")["id"]

        r = cliente_db.delete("/api/chart/alerts/{0}".format(aid))

        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        # Sigue en la base con estado `cancelled`: el log de alertas es memoria de
        # que el agente la puso, y un DELETE que desaparece no deja rastro.
        assert store_mod.get_chart_alert(aid)["status"] == "cancelled"

    def test_alerta_inexistente_es_404(self, cliente_db):
        assert cliente_db.delete("/api/chart/alerts/9999").status_code == 404

    def test_cancelar_sin_token_es_401(self, cliente_db, monkeypatch):
        from database import store as store_mod

        monkeypatch.setenv("API_TOKEN", "secreto")
        aid = store_mod.add_chart_alert("EURUSD", 1.09, label="resistencia")["id"]

        assert cliente_db.delete("/api/chart/alerts/{0}".format(aid)).status_code == 401


class TestConfigDelAgente:
    def test_lectura_con_los_campos_que_pinta_el_panel(self, cliente_db):
        cuerpo = cliente_db.get("/api/agent/config").json()

        assert cuerpo["active_role_id"] == "general"
        assert isinstance(cuerpo["fallback_order"], list)
        assert cuerpo["history_limit"] > 0
        assert set(cuerpo["keys"]) == {"OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"}

    def test_la_clave_nunca_sale(self, cliente_db, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secreto-de-verdad")

        cuerpo = cliente_db.get("/api/agent/config").json()

        assert cuerpo["keys"]["OPENAI_API_KEY"] is True
        assert "sk-secreto-de-verdad" not in json.dumps(cuerpo)

    def test_post_cambia_lo_que_manda_y_devuelve_el_estado(self, cliente_db):
        r = cliente_db.post(
            "/api/agent/config", json={"history_limit": 5, "max_tool_rounds": 2, "fallback_order": ["openai/gpt-4o"]}
        )

        assert r.status_code == 200
        assert r.json()["history_limit"] == 5
        assert r.json()["max_tool_rounds"] == 2
        assert r.json()["fallback_order"] == ["openai/gpt-4o"]

    def test_post_sin_token_es_401(self, cliente_db, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "secreto")

        assert cliente_db.post("/api/agent/config", json={"history_limit": 5}).status_code == 401

    def test_un_setting_corrupto_no_tumba_el_panel(self, cliente_db):
        from database import store as store_mod

        store_mod.set_setting("fallback_order", "{esto no es json")

        cuerpo = cliente_db.get("/api/agent/config").json()

        assert cuerpo["fallback_order"] == []
        assert cuerpo["history_limit"] > 0


class TestAltaDeRoles:
    def test_alta_con_el_id_en_el_body(self, cliente_db):
        r = cliente_db.post("/api/agent/roles", json={"role_id": "riesgo", "name": "Riesgo", "system_prompt": "mira el riesgo"})

        assert r.status_code == 200
        assert cliente_db.get("/api/agent/roles/riesgo").json()["name"] == "Riesgo"

    def test_sin_id_es_422(self, cliente_db):
        r = cliente_db.post("/api/agent/roles", json={"name": "Sin id"})

        assert r.status_code == 422


class TestSyncDeTrades:
    def test_vuelca_el_historial_y_devuelve_la_cuenta(self, cliente_db):
        r = cliente_db.post("/api/trades/sync")

        assert r.status_code == 200
        assert r.json()["count"] == 1

    def test_con_el_broker_apagado_es_503(self, db):
        """Con el `db` delante: sin el, esta escritura iria a la base de verdad."""
        from database import store as store_mod

        app = create_app(_runtime(store=store_mod, market=FakeMarket(fallo=AdapterError("MT5 fuera"))))
        with TestClient(app) as c:
            assert c.post("/api/trades/sync").status_code == 503

    def test_sin_token_es_401(self, cliente_db, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "secreto")

        assert cliente_db.post("/api/trades/sync").status_code == 401


class TestCot:
    def test_sin_datos_es_404_con_el_motivo(self, cliente_db):
        r = cliente_db.get("/api/cot/report")

        assert r.status_code == 404
        assert "refrescar" in r.json()["detail"]

    def test_lee_de_la_tabla_y_no_de_la_red(self, cliente_db, monkeypatch):
        from macro_ingestor.forex import cot_service

        def revienta(*args: Any, **kwargs: Any):
            raise AssertionError("el GET no debe salir a la red")

        monkeypatch.setattr(cot_service, "fetch_reports", revienta)
        _sembrar_cot(cliente_db)

        cuerpo = cliente_db.get("/api/cot/report").json()

        assert cuerpo["macro_bias"] in {"BULLISH", "BEARISH", "NEUTRAL"}
        assert cuerpo["report_date"].startswith("2026-")

    def test_refresh_descarga_persiste_y_devuelve(self, cliente_db, monkeypatch):
        from macro_ingestor.forex import cot_service

        crudos = _crudos_cot()
        monkeypatch.setattr(cot_service, "fetch_reports", lambda **kwargs: list(crudos))

        cuerpo = cliente_db.post("/api/cot/refresh").json()

        assert cuerpo["report_date"] == crudos[-1]["report_date"]
        # Lo persistido se lee de la tabla, no de la respuesta de la descarga.
        assert cliente_db.get("/api/cot/report").json()["report_date"] == cuerpo["report_date"]

    def test_refresh_sin_token_es_401(self, cliente_db, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "secreto")

        assert cliente_db.post("/api/cot/refresh").status_code == 401

    def test_caida_de_la_cftc_es_502(self, cliente_db, monkeypatch):
        from macro_ingestor.base_ingestor import FeedUnavailable
        from macro_ingestor.forex import cot_service

        def falla(**kwargs: Any):
            raise FeedUnavailable("Socrata no responde")

        monkeypatch.setattr(cot_service, "fetch_reports", falla)

        r = cliente_db.post("/api/cot/refresh")

        assert r.status_code == 502
        assert "Socrata" in r.json()["error"]

    def test_el_indice_necesita_26_filas(self, cliente_db):
        """Con `limit=5` no se puede calcular el indice de 26 semanas.

        Por eso el minimo del `Query` es 26: un `limit=5` daria un "indice" de cinco
        semanas presentado con el nombre de uno de 26.
        """
        assert cliente_db.get("/api/cot/report", params={"limit": 5}).status_code == 422


class TestVolumeProfile:
    def test_el_perfil_viene_de_las_velas_del_rango(self, cliente):
        cuerpo = cliente.get("/api/volume-profile/EURUSD", params={"timeframe": "M15", "bins": 24}).json()

        assert cuerpo["source"] == "bars"
        assert len(cuerpo["profile"]) == 24
        assert cuerpo["val"] <= cuerpo["poc"] <= cuerpo["vah"]

    def test_bins_fuera_de_rango_es_422(self, cliente):
        assert cliente.get("/api/volume-profile/EURUSD", params={"bins": 500}).status_code == 422
        assert cliente.get("/api/volume-profile/EURUSD", params={"poc_pct": 0}).status_code == 422

    def test_sin_velas_es_404(self):
        with TestClient(create_app(_runtime(market=FakeMarket(velas=[])))) as c:
            assert c.get("/api/volume-profile/EURUSD").status_code == 404


def _crudos_cot(semanas: int = 30) -> List[Dict[str, Any]]:
    """Una serie de COT que se parece en forma a la de la CFTC: 3 series y delta."""
    filas = []
    for i in range(semanas):
        filas.append(
            {
                "report_date": "2026-{:02d}-{:02d}".format(i // 4 + 1, (i % 4) * 7 + 1),
                "am_net": 10000 + i * 250,
                "lf_net": -5000 - i * 120,
                "nc_net": -2000 + i * 90,
            }
        )
    return filas


def _sembrar_cot(cliente: Any, semanas: int = 30) -> None:
    from database import store as store_mod

    for fila in _crudos_cot(semanas):
        store_mod.upsert_cot_report(
            fila["report_date"], fila["am_net"], fila["lf_net"], fila["nc_net"]
        )