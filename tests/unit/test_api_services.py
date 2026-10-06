"""Tests de los servicios de la API que no dependen del terminal.

Alcance
-------
`BrokerClock`, `MacroNews` y `MT5Exports` son las tres piezas que se pueden probar
enteras sin bróker: el reloj solo necesita una config y una carpeta, el calendario
acepta un `poll` inyectado y las exportaciones viven en un `tmp_path`.

Qué se afirma y por qué importa
-------------------------------
1. El reloj: el día de trading es la fecha del BROKER, y un reloj rancio o en
   desacuerdo con la config se AVISA en vez de elegir. Elegir en silencio resetea
   el día en el momento equivocado y reabre el hueco de `max_trades_day`.
2. Las noticias: si el calendario no se puede leer, el gate se abre pero lo
   DECLARA (`fail_open`). Un gate fail-open mudo es indistinguible de uno que ha
   visto el calendario y no ha encontrado nada, y esa distinción es la que decide
   si se opera o no.
3. Las exportaciones: un endpoint que lee ficheros por nombre es un path traversal
   esperando, y "no hay directorio" no es lo mismo que "no hay ficheros".
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from api.services.broker_clock import (
    EA_CLOCK_FILE,
    EA_CLOCK_MAX_AGE_S,
    BrokerClock,
)
from api.services.macro_news import CALENDAR_SOURCE, MacroNews
from api.services.mt5_exports import (
    ExportDirMissing,
    ExportNotFound,
    MT5Exports,
    parse_filename,
)
from core import paths as core_paths

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Utilidades compartidas
# ---------------------------------------------------------------------------

def _utc(texto: str) -> datetime:
    return datetime.strptime(texto, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


class StoreStub:
    """El mínimo de `database.store` que leen estos servicios."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None, cot: Optional[List[Any]] = None) -> None:
        self._cfg = cfg if cfg is not None else {}
        self._cot = cot or []
        self.cfg_calls = 0

    def get_trading_config(self) -> Dict[str, Any]:
        self.cfg_calls += 1
        return self._cfg

    def list_cot_reports(self, *args: Any, **kwargs: Any) -> List[Any]:
        return self._cot


class _ExplodingStore:
    """Un store que falla al leer la config: los defaults tienen que salir."""

    def get_trading_config(self) -> Dict[str, Any]:
        raise RuntimeError("base no disponible")

    def list_cot_reports(self, *args: Any, **kwargs: Any) -> List[Any]:
        raise RuntimeError("base no disponible")


@pytest.fixture
def cfg_helsinki() -> Dict[str, Any]:
    """La config REAL del repo, para que el reloj se pruebe con lo de producción."""
    with open(core_paths.STRATEGY_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@pytest.fixture(autouse=True)
def _sin_terminal_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Que el reloj NO pueda leer el terminal de esta máquina.

    `BrokerClock._dirs_candidatos()` añade SIEMPRE `%APPDATA%/MetaQuotes/Terminal/Common/Files`
    además del directorio inyectado, y en un portátil con MT5 instalado y el EA
    corriendo hay un `ILOF_clock.json` real ahí. Sin esto, el test "sin EA
    verifica=false" leería el reloj del broker de verdad y pasaría/fallaría según
    la sesión de trading del usuario: la suite tiene que ser la misma en un
    portátil con bróker que en un CI sin nada.
    """
    monkeypatch.delenv("APPDATA", raising=False)


# ---------------------------------------------------------------------------
# BrokerClock
# ---------------------------------------------------------------------------

class TestBrokerClockZona:
    def test_zona_de_la_config_real(self, cfg_helsinki: Dict[str, Any]) -> None:
        """`clock.broker_tz` decide la fecha del día de trading.

        Es `Europe/Helsinki` en el YAML de producción, no una constante: si el día
        se calculase en UTC, el cutoff de operaciones cruzaría el día a una hora
        distinta de la que usa el EA.
        """
        reloj = BrokerClock(cfg=lambda: cfg_helsinki)

        assert reloj.tz_name() == cfg_helsinki["clock"]["broker_tz"]

    def test_offset_fijo_gana_sobre_la_zona(self) -> None:
        """Un offset explícito es para brokers SIN horario de verano."""
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_tz": "Europe/Helsinki", "broker_utc_offset_minutes": 180}})

        assert reloj.tz_name() == "UTC+180"
        assert reloj.offset_minutes(_utc("2026-01-15 12:00")) == 180

    def test_offset_fijo_no_cambia_con_el_dst(self) -> None:
        """La razón de existir del offset fijo: en verano sigue siendo el mismo.

        Helsinki en invierno es UTC+2 y en verano UTC+3. Un broker con horario fijo
        no hace ese cambio, así que leer la zona daría el desfase equivocado media
        parte del año.
        """
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_tz": "Europe/Helsinki", "broker_utc_offset_minutes": 180}})

        invierno = reloj.offset_minutes(_utc("2026-01-15 12:00"))
        verano = reloj.offset_minutes(_utc("2026-07-15 12:00"))

        assert invierno == verano == 180

    def test_zona_desconocida_cae_a_utc(self) -> None:
        """Una zona mal escrita no puede tumbar la API entera."""
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_tz": "Marte/Olympus"}})

        assert reloj.tzinfo().utcoffset(None) == timedelta(0)

    def test_config_que_falla_usa_el_default(self) -> None:
        """Sin config se opera con UTC, que es al menos una respuesta honesta."""
        reloj = BrokerClock(cfg=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        assert reloj.tz_name() == "UTC"

    def test_offset_no_numérico_no_revienta(self) -> None:
        """Una config editada a mano con texto donde va un número no es un 500."""
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_utc_offset_minutes": "tres"}})

        assert reloj.offset_minutes(_utc("2026-01-15 12:00")) == 0

    def test_sin_proveedor_usa_utc(self) -> None:
        """`BrokerClock()` a secas es un reloj UTC, no un error de construcción."""
        assert BrokerClock().trading_day(_utc("2026-03-01 23:30")) == "2026-03-01"


class TestBrokerClockDiaDeTrading:
    def test_dia_de_trading_es_la_fecha_del_broker(self, cfg_helsinki: Dict[str, Any]) -> None:
        """A las 22:30 UTC en invierno, Helsinki ya es al día siguiente."""
        reloj = BrokerClock(cfg=lambda: cfg_helsinki)

        assert reloj.trading_day(_utc("2026-01-15 22:30")) == "2026-01-16"

    def test_dia_de_trading_no_es_la_fecha_utc(self, cfg_helsinki: Dict[str, Any]) -> None:
        """La diferencia que separa este reloj de `core.clock`."""
        reloj = BrokerClock(cfg=lambda: cfg_helsinki)
        momento = _utc("2026-01-15 22:30")

        assert reloj.trading_day(momento) != momento.strftime("%Y-%m-%d")

    def test_limites_del_dia_en_utc(self, cfg_helsinki: Dict[str, Any]) -> None:
        """(inicio, fin) en UTC aware, que es lo que espera `history_deals_get`.

        Un naive lo interpreta MT5 como hora local del SO, que en un portátil en
        hora de verano desplazaría el rango una hora.
        """
        reloj = BrokerClock(cfg=lambda: cfg_helsinki)

        inicio, fin = reloj.trading_day_bounds(_utc("2026-01-15 12:00"))

        assert inicio.tzinfo is not None and fin.tzinfo is not None
        assert inicio.astimezone(reloj.tzinfo()).strftime("%Y-%m-%d %H:%M") == "2026-01-15 00:00"
        assert (fin - inicio) == timedelta(days=1)

    def test_ventana_de_7_dias_incluye_el_dia_en_curso(self, cfg_helsinki: Dict[str, Any]) -> None:
        """El día de HOY cuenta completo aunque no haya terminado.

        Con `now - 7 días` a secas, a las 09:00 solo habría 6 días y medio de
        historial: se perderían las operaciones de la tarde del día anterior, que
        es justo cuando se opera.
        """
        reloj = BrokerClock(cfg=lambda: cfg_helsinki)
        ahora = _utc("2026-01-15 09:00")

        inicio, fin = reloj.trading_day_window(7, ahora)

        assert fin == ahora
        inicio_esperado = reloj.trading_day_bounds(ahora)[0] - timedelta(days=7)
        assert inicio == inicio_esperado

    def test_ventana_de_0_dias_es_el_dia_en_curso(self, cfg_helsinki: Dict[str, Any]) -> None:
        """`days=0` es "hoy", no "ahora mismo"."""
        reloj = BrokerClock(cfg=lambda: cfg_helsinki)
        ahora = _utc("2026-01-15 09:00")

        inicio, fin = reloj.trading_day_window(0, ahora)

        assert inicio == reloj.trading_day_bounds(ahora)[0]
        assert fin == ahora

    def test_ventana_con_dias_negativos_no_va_al_futuro(self, cfg_helsinki: Dict[str, Any]) -> None:
        """Un `days` negativo viene de una config editada a mano: no se corrige a futuro."""
        reloj = BrokerClock(cfg=lambda: cfg_helsinki)
        ahora = _utc("2026-01-15 09:00")

        inicio, _ = reloj.trading_day_window(-3, ahora)

        assert inicio == reloj.trading_day_bounds(ahora)[0]


class TestBrokerClockContraElEA:
    def test_sin_ea_no_verifica_pero_opera(self, tmp_path: Path) -> None:
        """`verified: false` es un aviso, no un error.

        Nadie ha comprobado contra el broker, pero la config es lo declarado y
        bloquear la operación aquí dejaría al bot sin operar sin explicación.
        """
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_tz": "Europe/Helsinki"}}, files_dir=lambda: str(tmp_path))

        estado = reloj.state(_utc("2026-01-15 12:00"))

        assert estado["verified"] is False
        assert estado["ea_clock"] is None
        assert estado["trading_day"] == "2026-01-15"

    def test_ea_de_acuerdo_verifica(self, tmp_path: Path) -> None:
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_utc_offset_minutes": 120}}, files_dir=lambda: str(tmp_path))
        _escribe_ea(tmp_path, offset_minutes=120, trading_day="2026-01-15")

        estado = reloj.state(_utc("2026-01-15 12:00"))

        assert estado["verified"] is True
        assert estado["server_offset_minutes"] == 120
        assert estado["ea_clock"]["trading_day"] == "2026-01-15"

    def test_un_minuto_de_diferencia_no_es_mismatch(self, tmp_path: Path) -> None:
        """Un minuto es redondeo del servidor, no otra zona horaria."""
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_utc_offset_minutes": 120}}, files_dir=lambda: str(tmp_path))
        _escribe_ea(tmp_path, offset_minutes=121)

        estado = reloj.state(_utc("2026-01-15 12:00"))

        assert estado["verified"] is True

    def test_diferencia_grande_se_avisa_y_no_se_elige(self, tmp_path: Path) -> None:
        """El desacuerdo se PUBLICA; el reloj sigue con la config declarada.

        Elegir aquí en silencio resetearía el día de trading en el momento
        equivocado, y con él el HWM del prop firm y el contador de operaciones.
        """
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_utc_offset_minutes": 120}}, files_dir=lambda: str(tmp_path))
        _escribe_ea(tmp_path, offset_minutes=300, trading_day="2026-01-14")

        estado = reloj.state(_utc("2026-01-15 12:00"))

        assert estado["verified"] is False
        assert "mismatch" in estado
        assert estado["trading_day"] == "2026-01-15", "manda la config declarada"
        assert estado["server_offset_minutes"] == 300

    def test_reloj_rancio_no_verifica(self, tmp_path: Path) -> None:
        """Un fichero viejo es la foto de una sesión anterior, no el estado.

        Con la terminal cerrada el EA deja de escribir y el fichero se queda ahí:
        creyéndolo, el desfase se compararía contra la hora de ayer.
        """
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_utc_offset_minutes": 120}}, files_dir=lambda: str(tmp_path))
        _escribe_ea(tmp_path, offset_minutes=999, mtime_age_s=EA_CLOCK_MAX_AGE_S + 60)

        estado = reloj.state(_utc("2026-01-15 12:00"))

        assert estado["verified"] is False
        assert estado["ea_clock"]["stale"] is True
        assert "mismatch" not in estado

    def test_ea_ilegible_no_rompe(self, tmp_path: Path) -> None:
        """Medio fichero de un EA que se cerró a mitad de escritura."""
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_tz": "UTC"}}, files_dir=lambda: str(tmp_path))
        (tmp_path / EA_CLOCK_FILE).write_text("{no json", encoding="utf-8")

        assert reloj.read_ea_clock() is None
        assert reloj.state(_utc("2026-01-15 12:00"))["ea_clock"] is None

    def test_offset_absurdo_se_ignora(self, tmp_path: Path) -> None:
        """Un `offset_minutes` de 99999 no es un desfase: es basura."""
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_utc_offset_minutes": 120}})
        _escribe_ea(tmp_path, offset_minutes=99999)

        assert reloj.state(_utc("2026-01-15 12:00"))["verified"] is False

    def test_json_que_no_es_objeto_no_rompe(self, tmp_path: Path) -> None:
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_tz": "UTC"}}, files_dir=lambda: str(tmp_path))
        (tmp_path / EA_CLOCK_FILE).write_text("[1, 2, 3]", encoding="utf-8")

        assert reloj.read_ea_clock() is None

    def test_files_dir_que_falla_no_rompe(self) -> None:
        """Sin terminal no hay ficheros que leer, y eso no es una excepción."""
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_tz": "UTC"}}, files_dir=lambda: (_ for _ in ()).throw(RuntimeError("no MT5")))

        assert reloj.read_ea_clock() is None

    def test_busca_en_files_y_en_la_raiz(self, tmp_path: Path) -> None:
        """La API expone `commondata_path` en dos formas según la versión.

        El EA escribe en FILE_COMMON, que es `<commondata>/Files` en unas versiones
        y directamente `<commondata>` en otras. Buscar solo una deja el reloj sin
        verificar en la mitad de las instalaciones.
        """
        comun = tmp_path / "comun"
        comun.mkdir()
        (comun / "Files").mkdir()
        _escribe_ea(comun / "Files", offset_minutes=120, trading_day="2026-01-15")
        reloj = BrokerClock(cfg=lambda: {"clock": {"broker_utc_offset_minutes": 120}}, files_dir=lambda: str(comun))

        assert reloj.read_ea_clock()["trading_day"] == "2026-01-15"


def _escribe_ea(
    carpeta: Path,
    offset_minutes: int,
    trading_day: str = "2026-01-15",
    mtime_age_s: float = 0.0,
) -> Path:
    """Escribe el fichero que publicaría el EA, con la edad que se le pida."""
    ruta = carpeta / EA_CLOCK_FILE
    ruta.write_text(
        json.dumps(
            {
                "offset_minutes": offset_minutes,
                "trading_day": trading_day,
                "trades_today": 2,
                "server_time": "2026-01-15 14:00:00",
                "build": 4500,
            }
        ),
        encoding="utf-8",
    )
    if mtime_age_s:
        import time

        cuando = time.time() - mtime_age_s
        os.utime(ruta, (cuando, cuando))
    return ruta


# ---------------------------------------------------------------------------
# MacroNews
# ---------------------------------------------------------------------------

class TestMacroNewsGate:
    def test_verde_sin_noticias(self) -> None:
        """El caso normal: el calendario se leyó y no hay nada que bloquee."""
        serv = MacroNews(poll=lambda **kw: {"source": CALENDAR_SOURCE, "payload": {"ok": True, "block": None}, "stale": False})

        gate = serv.gate()

        assert gate["ok"] is True
        assert gate["block"] is None
        assert gate["fail_open"] is False

    def test_bloquea_con_noticia_de_alto_impacto(self) -> None:
        serv = MacroNews(
            poll=lambda **kw: {
                "source": CALENDAR_SOURCE,
                "payload": {"ok": True, "block": "FOMC", "detail": ["23:45 FOMC"]},
                "stale": False,
            }
        )

        gate = serv.gate()

        assert gate["block"] == "FOMC"
        assert serv.blocked() is True

    def test_fallo_de_red_abre_la_puerta_pero_lo_declara(self) -> None:
        """`fail_open` es la diferencia entre "no hay noticia" y "no lo sé".

        Un gate que se abre callado es indistinguible del que ha visto el
        calendario limpio, y quien decide si se opera no puede distinguirlos.
        """
        serv = MacroNews(poll=lambda **kw: (_ for _ in ()).throw(OSError("sin DNS")))

        gate = serv.gate()

        assert gate["block"] is None
        assert serv.blocked() is False
        assert gate["fail_open"] is True
        assert "OSError" in gate["reason"]

    def test_news_desactivado_no_pide_red(self) -> None:
        """Con `data_sources.news: false` el calendario no se consulta ni se intenta."""
        llamado = []

        def poll(**kw):  # pragma: no cover - no debe llegar a llamarse
            llamado.append(kw)
            return {}

        serv = MacroNews(store=StoreStub({"data_sources": {"news": False}}), poll=poll)

        assert serv.blocked() is False
        assert llamado == []

    def test_desactivado_no_es_un_fallo(self) -> None:
        """`fail_open` distingue "no lo sé" de "lo he apagado yo".

        Confundir los dos haría que un gate apagado a propósito gritara
        `fail_open` en cada snapshot y el operador acabara ignorando el aviso que
        sí importa.
        """
        serv = MacroNews(store=StoreStub({"data_sources": {"news": False}}))

        gate = serv.gate()

        assert gate["fail_open"] is False
        assert gate["disabled"] is True
        assert gate["ok"] is True
        assert gate["block"] is None

    def test_calendario_no_implementado_abre_mas_lo_dice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin fuente implementada no hay veredicto: se abre DICIÉNDOlo.

        Es el otro camino del mismo problema que D-025 evita: un `neutro` con la
        puerta abierta callada es indistinguible de un calendario limpio. Se
        parchea el registro (no la red) para provocar el caso sin salir a Internet.
        """
        monkeypatch.setattr("macro_ingestor.registry.resolve", lambda nombre: None)

        gate = MacroNews().gate()

        assert gate["fail_open"] is True
        assert "no está implementado" in gate["reason"]

    def test_un_poll_que_devuelve_nada_no_tumba_el_gate(self) -> None:
        """Una fuente que rompe el contrato del puerto no puede ser un 500.

        `poll` devolviendo `None` no es un caso previsto, pero un `AttributeError`
        en el gate convertiría un calendario raro en la caída de `/api/clock` y del
        watcher entero.
        """
        gate = MacroNews(poll=lambda **kw: None).gate()

        assert gate["fail_open"] is True
        assert gate["reason"] == "el calendario no devolvió veredicto"

    def test_lectura_sin_payload_no_revienta(self) -> None:
        """Una fuente que devuelve algo raro tiene que degradar, no reventar."""
        serv = MacroNews(poll=lambda **kw: {"source": CALENDAR_SOURCE})

        gate = serv.gate()

        assert gate["ok"] is True
        assert gate["reason"]

    def test_el_buffer_min_viene_de_la_config(self) -> None:
        """La ventana del gate se lee del YAML NIDADO.

        REF leía `cfg.get("news_buffer_min")`, clave plana que no existe en
        `strategy.yaml`: el gate corría con el default sin que nadie lo notara.
        """
        visto: Dict[str, Any] = {}

        def poll(**kw):
            visto.update(kw)
            return {"source": CALENDAR_SOURCE, "payload": {"ok": True}}

        store = StoreStub({"execution": {"news_buffer_min": 45, "news_gate_impact": "orange"}})
        MacroNews(store=store, poll=poll).gate()

        assert visto == {"buffer_min": 45, "min_impact": "orange"}

    def test_config_incompleta_usa_los_defaults(self) -> None:
        """Sin `execution` en el YAML: el default declarado, no una excepción."""
        visto: Dict[str, Any] = {}

        def poll(**kw):
            visto.update(kw)
            return {"source": CALENDAR_SOURCE, "payload": {"ok": True}}

        MacroNews(store=StoreStub({}), poll=poll).gate()

        assert visto["buffer_min"] == 15
        assert visto["min_impact"] == "red"

    def test_config_que_falla_no_tumba_el_gate(self) -> None:
        MacroNews(store=_ExplodingStore(), poll=lambda **kw: {"source": CALENDAR_SOURCE, "payload": {"ok": True}}).gate()


class TestMacroNewsOtrasFuentes:
    def test_sin_store_el_cot_es_none(self) -> None:
        """Sin base no hay COT, y eso no es un error: es un score sin ese extra."""
        assert MacroNews().cot() is None

    def test_cot_que_falla_es_none(self) -> None:
        assert MacroNews(store=_ExplodingStore()).cot() is None

    def test_lecturas_de_una_fuente_sin_implementacion(self) -> None:
        """Un nombre sin fuente vuelve VACÍO y con motivo, no con silencio (D-027).

        El payload es `None` a propósito: la fuente no dio nada, y rellenar el hueco
        con `{"ok": False}` haría creer que la fuente respondió y se equivocó.
        """
        resultado = MacroNews().lecturas(["no_existe_esta_fuente"])

        assert resultado["no_existe_esta_fuente"]["payload"] is None
        assert "sin implementación" in resultado["no_existe_esta_fuente"]["reason"]


# ---------------------------------------------------------------------------
# MT5Exports
# ---------------------------------------------------------------------------

@pytest.fixture
def exports_dir(tmp_path: Path) -> Path:
    carpeta = tmp_path / "Files"
    carpeta.mkdir()
    return carpeta


@pytest.fixture
def exports(exports_dir: Path) -> MT5Exports:
    return MT5Exports(files_dir=lambda: str(exports_dir))


class TestParseFilename:
    @pytest.mark.parametrize(
        "nombre,simbolo,tf,modo",
        [
            ("AR_AI_ADV_EURUSD_M15_Full_Trade_Plan.txt", "EURUSD", "M15", "Full Trade Plan"),
            ("AR_AI_ADV_GBPUSD_H1_Analyze_Chart.txt", "GBPUSD", "H1", "Analyze Chart"),
            ("AR_AI_ADV_EURUSD_M5_Manual.txt", "EURUSD", "M5", "Manual"),
            ("AR_AI_ADV_EURUSD_M5_Quick_Default_Analyze.txt", "EURUSD", "M5", "Quick Default Analyze"),
        ],
    )
    def test_nombres_del_indicador(self, nombre: str, simbolo: str, tf: str, modo: str) -> None:
        """El formato lo escribe el indicador, no nosotros: se parsea tal cual."""
        meta = parse_filename(nombre)

        assert meta["symbol"] == simbolo
        assert meta["timeframe"] == tf
        assert meta["mode"] == modo

    def test_nombre_sin_prefijo_tambien_se_parsea(self) -> None:
        meta = parse_filename("EURUSD_H4.txt")

        assert meta["symbol"] == "EURUSD"
        assert meta["timeframe"] == "H4"
        assert meta["mode"] is None

    def test_nombre_basura_no_inventa_datos(self) -> None:
        """El parser es PERMISIVO: un token con forma de símbolo se acepta como tal.

        `lo_que_sea` parte en `LO` + `que` + `sea`, y `LO` encaja con la forma de un
        símbolo de dos letras. Se documenta porque es lo que hace, no porque sea lo
        deseable: el nombre lo genera el indicador, y un filtro de exports ya
        descarta por prefijo lo que no venga de él.
        """
        meta = parse_filename("lo_que_sea.txt")

        assert meta["symbol"] == "LO"
        assert meta["timeframe"] is None
        assert meta["mode"] is None


class TestListExports:
    def test_directorio_ausente_no_es_lista_vacia(self, tmp_path: Path) -> None:
        """"No hay directorio" y "no hay ficheros" llevan a respuestas distintas.

        Devolver `[]` haría que el agente dijera "el indicador no ha exportado
        nada" cuando en realidad es que no encuentra MT5.
        """
        serv = MT5Exports(files_dir=lambda: str(tmp_path / "no_existe"))

        with pytest.raises(ExportDirMissing):
            serv.list_exports()

    def test_directorio_vacio_es_lista_vacia(self, exports: MT5Exports) -> None:
        assert exports.list_exports() == []

    def test_filtra_por_simbolo(self, exports: MT5Exports, exports_dir: Path) -> None:
        (exports_dir / "AR_AI_ADV_EURUSD_M15_Manual.txt").write_text("eur", encoding="utf-8")
        (exports_dir / "AR_AI_ADV_GBPUSD_M15_Manual.txt").write_text("gbp", encoding="utf-8")

        assert [e["filename"] for e in exports.list_exports(symbol="eurusd")] == [
            "AR_AI_ADV_EURUSD_M15_Manual.txt"
        ]

    def test_ignora_ficheros_que_no_son_del_indicador(self, exports: MT5Exports, exports_dir: Path) -> None:
        """La carpeta `MQL5/Files` es compartida: hay mil cosas que no son exports."""
        (exports_dir / "notas.txt").write_text("hola", encoding="utf-8")
        (exports_dir / "AR_AI_ADV_EURUSD_M15_Manual.txt").write_text("eur", encoding="utf-8")

        assert [e["filename"] for e in exports.list_exports()] == ["AR_AI_ADV_EURUSD_M15_Manual.txt"]

    def test_ordena_de_mas_nueva_a_mas_vieja(self, exports: MT5Exports, exports_dir: Path) -> None:
        viejo = exports_dir / "AR_AI_ADV_EURUSD_M15_Manual.txt"
        nuevo = exports_dir / "AR_AI_ADV_EURUSD_H1_Manual.txt"
        viejo.write_text("viejo", encoding="utf-8")
        nuevo.write_text("nuevo", encoding="utf-8")
        os.utime(viejo, (1_700_000_000, 1_700_000_000))
        os.utime(nuevo, (1_700_000_500, 1_700_000_500))

        assert [e["filename"] for e in exports.list_exports()] == [
            "AR_AI_ADV_EURUSD_H1_Manual.txt",
            "AR_AI_ADV_EURUSD_M15_Manual.txt",
        ]

    def test_limita_las_entradas(self, exports: MT5Exports, exports_dir: Path) -> None:
        for i in range(5):
            nombre = "AR_AI_ADV_EURUSD_M{0}_Manual.txt".format(i + 1)
            (exports_dir / nombre).write_text("x", encoding="utf-8")

        assert len(exports.list_exports(limit=3)) == 3


class TestReadExport:
    def test_lee_el_contenido(self, exports: MT5Exports, exports_dir: Path) -> None:
        (exports_dir / "AR_AI_ADV_EURUSD_M15_Manual.txt").write_text("Sesgo: alcista", encoding="utf-8")

        resultado = exports.read_export("AR_AI_ADV_EURUSD_M15_Manual.txt")

        assert resultado["content"] == "Sesgo: alcista"
        assert resultado["truncated"] is False

    def test_trunca_y_lo_declara(self, exports: MT5Exports, exports_dir: Path) -> None:
        """El truncado se DICE. Un análisis cortado por la mitad sin decirlo produce
        un diagnóstico sobre la mitad del diagnóstico."""
        (exports_dir / "AR_AI_ADV_EURUSD_M15_Manual.txt").write_text("x" * 500, encoding="utf-8")

        resultado = exports.read_export("AR_AI_ADV_EURUSD_M15_Manual.txt", max_bytes=100)

        assert len(resultado["content"]) == 100
        assert resultado["truncated"] is True
        assert resultado["size"] == 500

    def test_fichero_inexistente(self, exports: MT5Exports) -> None:
        with pytest.raises(ExportNotFound):
            exports.read_export("AR_AI_ADV_EURUSD_M15_Manual.txt")

    def test_nombre_vacio(self, exports: MT5Exports) -> None:
        with pytest.raises(ExportNotFound):
            exports.read_export("   ")

    def test_directorio_ausente(self, tmp_path: Path) -> None:
        with pytest.raises(ExportDirMissing):
            MT5Exports(files_dir=lambda: str(tmp_path / "no_existe")).read_export("lo_que_sea.txt")


class TestPathTraversal:
    """Un endpoint que lee ficheros por nombre es un path traversal esperando."""

    @pytest.mark.parametrize(
        "nombre",
        [
            "../../secreto.txt",
            "..\\..\\secreto.txt",
            "sub/../../../secreto.txt",
            "....//....//secreto.txt",
        ],
    )
    def test_no_sale_del_directorio(self, exports: MT5Exports, nombre: str) -> None:
        with pytest.raises(ExportNotFound):
            exports.resolve_path(nombre)

    def test_no_acepta_una_ruta_absoluta(self, exports: MT5Exports, tmp_path: Path) -> None:
        fuera = tmp_path / "secreto.txt"
        fuera.write_text("no me leas", encoding="utf-8")

        with pytest.raises(ExportNotFound):
            exports.resolve_path(str(fuera))

    def test_un_subdirectorio_no_cuenta_como_fichero(self, exports: MT5Exports, exports_dir: Path) -> None:
        (exports_dir / "sub").mkdir()

        with pytest.raises(ExportNotFound):
            exports.resolve_path("sub")


class TestLatestExport:
    def test_sin_exportaciones_es_none(self, exports: MT5Exports) -> None:
        """`None`, no excepción: nadie ha pulsado Export es un estado normal."""
        assert exports.latest_export() is None

    def test_devuelve_la_mas_reciente(self, exports: MT5Exports, exports_dir: Path) -> None:
        viejo = exports_dir / "AR_AI_ADV_EURUSD_M15_Manual.txt"
        nuevo = exports_dir / "AR_AI_ADV_EURUSD_H1_Manual.txt"
        viejo.write_text("viejo", encoding="utf-8")
        nuevo.write_text("nuevo", encoding="utf-8")
        os.utime(viejo, (1_700_000_000, 1_700_000_000))
        os.utime(nuevo, (1_700_000_500, 1_700_000_500))

        assert exports.latest_export()["content"] == "nuevo"


class TestExtractExportPath:
    def test_saca_el_nombre_de_un_texto_pegado(self) -> None:
        """El usuario pega la ruta en el chat y tiene que funcionar tal cual."""
        texto = "mira esto: C:\\Users\\x\\MQL5\\Files\\AR_AI_ADV_EURUSD_M15_Manual.txt, dime el sesgo"

        # `extract_export_path` es un regex sobre texto: no toca el disco.
        assert MT5Exports().extract_export_path(texto) == "AR_AI_ADV_EURUSD_M15_Manual.txt"

    def test_texto_sin_exportacion(self, exports: MT5Exports) -> None:
        assert exports.extract_export_path("¿qué ves en el gráfico?") is None

    def test_texto_vacio(self, exports: MT5Exports) -> None:
        assert exports.extract_export_path("") is None