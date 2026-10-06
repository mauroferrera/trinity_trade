"""El reloj del broker: día de trading, hora de pared y verificación contra el EA.

Por qué NO está en `core/`
--------------------------
`core/clock.py` es pura y da la hora UTC canónica. El día de trading NO es la
fecha UTC: es la fecha del servidor del broker, que es donde el EA aplica
`TimeTradeServer()`. Si los dos cuentan días distintos, `max_trades_day` deja de
ser un tope compartido entre el EA y el bot y el reset del HWM del prop firm
cae en el momento equivocado. Por eso la zona del broker es un dato de
CONFIGURACIÓN (`clock.broker_tz` en `strategy.yaml`), no una constante del código.

Qué cambia respecto a REF
-------------------------
`REF/tclock.py` (418 líneas) hacía tres cosas y las tres eran legítimas: el reloj,
la verificación contra el EA, y la lectura del fichero. Lo que no lo era era
`_common_files_dirs()`, que hacía `import MetaTrader5` y `mt5.terminal_info()`
DIRECTO desde el módulo — una cuarta puerta al terminal, D-017 por la puerta
atrás, con su propio ciclo de vida y sin lock.

Aquí la lista de directorios llega por inyección (`files_dir`), que en producción
es `adapter.terminal_files_dir()`: la sesión del proceso, la única puerta. El
`ZoneInfo` es `core/clock` más la config, sin tocar el terminal.

Los dos caminos que se pierden al no hablar con el terminal:
- Sin EA publicando, `verified` queda en `False` y se opera igual. Es un aviso,
  no un error: la config es la fuente declarada.
- Con el EA publicando y en desacuerdo, se AVISA (`mismatch`) en vez de elegir.
  Elegir en silencio significa resetear el día de trading sin querer.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import clock

UTC = timezone.utc

#: Fichero que escribe el EA en FILE_COMMON con `TimeTradeServer()` y `TimeGMT()`.
EA_CLOCK_FILE = "ILOF_clock.json"

#: Un reloj más viejo que esto no dice nada del broker ahora mismo: el EA dejó de
#: escribir (terminal cerrada, EA sin permiso de ficheros, gráfico sin ticks) y lo
#: que hay en el disco es la última foto de una sesión anterior.
EA_CLOCK_MAX_AGE_S = 300.0

#: Tolerancia al comparar el offset del EA con el configurado, en minutos. Un
#: desfase de un minuto es redondeo del servidor, no una zona distinta.
EA_CLOCK_TOLERANCE_MIN = 1

DEFAULT_BROKER_UTC_OFFSET_MINUTES = 0
DEFAULT_BROKER_TZ = "UTC"

CfgProvider = Callable[[], Dict[str, Any]]
FilesDir = Callable[[], Optional[str]]


def _clock_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """La sección `clock` de la config, con defaults explícitos."""
    seccion = (cfg or {}).get("clock") or {}
    offset = seccion.get("broker_utc_offset_minutes", DEFAULT_BROKER_UTC_OFFSET_MINUTES)
    try:
        offset = int(offset or 0)
    except (TypeError, ValueError):
        offset = DEFAULT_BROKER_UTC_OFFSET_MINUTES
    return {
        "broker_tz": str(seccion.get("broker_tz") or DEFAULT_BROKER_TZ),
        "broker_utc_offset_minutes": offset,
    }


class BrokerClock:
    """Hora del servidor del broker y el día de trading que se deriva de ella.

    `cfg` y `files_dir` son inyectables porque las dos respuestas que importan son
    "la config dice Helsinki" y "el EA dice UTC+3": un test tiene que poder fijar
    las dos sin un YAML real ni una terminal.
    """

    __slots__ = ("_cfg", "_files_dir", "_now")

    def __init__(
        self,
        cfg: Optional[CfgProvider] = None,
        files_dir: Optional[FilesDir] = None,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self._cfg = cfg
        self._files_dir = files_dir
        self._now = now if now is not None else clock.epoch

    # -- configuración ---------------------------------------------------------

    def config(self) -> Dict[str, Any]:
        if self._cfg is None:
            return _clock_cfg(None)
        try:
            return _clock_cfg(self._cfg())
        except Exception:  # noqa: BLE001 - sin config se usa el default, se dice abajo
            return _clock_cfg(None)

    # -- zona y hora ------------------------------------------------------------

    def tzinfo(self, at: Optional[datetime] = None) -> tzinfo:
        """La zona del broker.

        `broker_utc_offset_minutes > 0` GANA sobre `broker_tz`: es para brokers con
        horario fijo, donde un nombre de zona con DST aplicaría el cambio de hora
        que ese broker no hace. Con 0 manda la zona (que respeta el DST).
        """
        cfg = self.config()
        if cfg["broker_utc_offset_minutes"]:
            return timezone(timedelta(minutes=cfg["broker_utc_offset_minutes"]))
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415 - solo hace falta aquí

            return ZoneInfo(cfg["broker_tz"])
        except Exception:  # noqa: BLE001 - zona desconocida: UTC es mejor que reventar
            return UTC

    def tz_name(self, at: Optional[datetime] = None) -> str:
        cfg = self.config()
        if cfg["broker_utc_offset_minutes"]:
            return "UTC{0:+d}".format(cfg["broker_utc_offset_minutes"])
        return cfg["broker_tz"]

    def broker_now(self, at: Optional[datetime] = None) -> datetime:
        """Ahora mismo en la zona del broker (aware)."""
        base = clock.to_utc(at) if at is not None else clock.now_utc()
        return base.astimezone(self.tzinfo(base))

    def offset_minutes(self, at: Optional[datetime] = None) -> int:
        """Desfase del broker respecto a UTC, en minutos."""
        return int(self.broker_now(at).utcoffset().total_seconds() // 60)

    def fmt_broker(self, at: Optional[datetime] = None) -> str:
        return clock.fmt_wall(self.broker_now(at), "%Y-%m-%d %H:%M:%S", "")

    # -- día de trading ---------------------------------------------------------

    def trading_day(self, at: Optional[datetime] = None) -> str:
        """`'YYYY-MM-DD'` en la fecha del SERVIDOR del broker.

        Es la clave de todo lo que resetea por día: contador de operaciones, HWM
        del prop firm y balance inicial. El EA la deriva de la medianoche de su
        `TimeTradeServer()`; aquí de la misma frontera.
        """
        return self.broker_now(at).strftime("%Y-%m-%d")

    def trading_day_bounds(
        self, at: Optional[datetime] = None
    ) -> Tuple[datetime, datetime]:
        """(inicio, fin) del día de trading, en UTC aware.

        Se pasan tal cual a `history_deals_get`: el paquete MetaTrader5 respeta el
        `tzinfo` (un naive lo interpreta como hora local del SO, que es otra zona y
        desplaza el rango).
        """
        local = self.broker_now(at)
        inicio = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return inicio.astimezone(UTC), (inicio + timedelta(days=1)).astimezone(UTC)

    def trading_day_window(
        self, days: int, at: Optional[datetime] = None
    ) -> Tuple[datetime, datetime]:
        """Ventana de los últimos N días de trading, en UTC aware.

        El día de HOY cuenta COMPLETO aunque aún no haya terminado: un
        `now - N días` a secas trunca el día en curso y se quedan fuera las
        operaciones de la tarde, que es justo cuando se opera.
        """
        inicio_hoy, _ = self.trading_day_bounds(at)
        fin = clock.to_utc(at) if at is not None else clock.now_utc()
        return inicio_hoy - timedelta(days=max(0, int(days))), fin

    # -- verificación contra el EA ---------------------------------------------

    def _dirs_candidatos(self) -> List[Path]:
        dirs: List[Path] = []
        if self._files_dir is not None:
            try:
                base = self._files_dir()
            except Exception:  # noqa: BLE001 - sin terminal no hay ficheros que leer
                base = None
            if base:
                # La API de Python expone `commondata_path` apuntando al\Common\ en
                # unas versiones y ya a Common\Files en otras; se prueban los dos.
                dirs.append(Path(base) / "Files")
                dirs.append(Path(base))
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(Path(appdata) / "MetaQuotes" / "Terminal" / "Common" / "Files")
        return dirs

    def read_ea_clock(self) -> Optional[Dict[str, Any]]:
        """El reloj que publica el EA, o `None` si no hay ninguno usable.

        Devuelve `offset_minutes`, `trading_day`, `trades_today` y `age_sec`. Un
        fichero ilegible o rancio no es un error: `None` y el llamante sigue con
        la config, que es lo que se ha declarado como fuente.
        """
        for folder in self._dirs_candidatos():
            path = folder / EA_CLOCK_FILE
            try:
                if not path.is_file():
                    continue
                with path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            try:
                data["age_sec"] = max(0.0, time.time() - path.stat().st_mtime)
            except OSError:
                continue
            return data
        return None

    # -- el estado que se publica ----------------------------------------------

    def state(self, at: Optional[datetime] = None) -> Dict[str, Any]:
        """El estado del reloj, tal y como lo lee `/api/clock`.

        Existe para que un desfase de zona sea VISIBLE en la UI y no un misterio.
        `verified: false` significa "funciona con lo configurado, pero nadie lo ha
        comprobado contra el broker". `mismatch` significa que el broker dice una
        cosa y la config otra: se avisa en vez de elegir, porque elegir mal resetea
        el día de trading en el momento equivocado y reabre el hueco de
        `max_trades_day`.
        """
        ahora = clock.to_utc(at) if at is not None else clock.now_utc()
        configurado = self.offset_minutes(ahora)
        info: Dict[str, Any] = {
            "broker_tz": self.tz_name(ahora),
            "configured_offset_minutes": configurado,
            "server_offset_minutes": None,
            "verified": False,
            "trading_day": self.trading_day(ahora),
            "trading_day_start_utc": clock.fmt_utc(
                self.trading_day_bounds(ahora)[0], "%Y-%m-%d %H:%M", ""
            ),
            "utc_now": clock.fmt_utc(ahora),
            "broker_now": self.fmt_broker(ahora),
            "ea_clock": None,
        }
        ea = self.read_ea_clock()
        if not ea:
            return info
        try:
            offset_ea = int(ea.get("offset_minutes"))
        except (TypeError, ValueError):
            return info
        if not -1440 < offset_ea < 1440:
            return info
        edad = float(ea.get("age_sec") or 0.0)
        info["ea_clock"] = {
            "trading_day": ea.get("trading_day"),
            "offset_minutes": offset_ea,
            "trades_today": ea.get("trades_today"),
            "server_time": ea.get("server_time"),
            "build": ea.get("build"),
            "age_sec": round(edad, 1),
        }
        if edad > EA_CLOCK_MAX_AGE_S:
            info["ea_clock"]["stale"] = True
            return info
        info["server_offset_minutes"] = offset_ea
        if abs(offset_ea - configurado) <= EA_CLOCK_TOLERANCE_MIN:
            info["verified"] = True
        else:
            info["mismatch"] = "broker UTC{0:+d} vs config UTC{1:+d} ({2})".format(
                offset_ea, configurado, self.tz_name(ahora)
            )
        return info


__all__ = [
    "BrokerClock",
    "DEFAULT_BROKER_TZ",
    "DEFAULT_BROKER_UTC_OFFSET_MINUTES",
    "EA_CLOCK_FILE",
    "EA_CLOCK_MAX_AGE_S",
    "EA_CLOCK_TOLERANCE_MIN",
]
