"""La mitad de `strategy_map.yaml` que toca el fichero.

`core/strategy_map.py` sabe qué es un mapa válido y cómo se resuelve un perfil;
este módulo sabe dónde está el archivo y cómo se lee. Es el mismo reparto que
`settings/strategy_source.py` con `strategy.yaml`, y por el mismo motivo:
`core/` no puede abrir ficheros (test de pureza) y meter la lectura aquí ataría
`store` a una segunda fuente de verdad.

Una diferencia deliberada con `strategy_source`: el fichero NO es obligatorio.
`strategy.yaml` define si se opera y su ausencia tiene que sonar; el mapa, en
cambio, tiene un default con sentido —sin mapa, todo es el perfil "default",
que es EXACTAMENTE el comportamiento histórico de un solo YAML. Un fichero mal
formado sí revienta: callado, cualquier magic nuevo caería en "default" sin que
nadie lo decidiera.

Nombres públicos para `store`: `load` y `invalidate`, con la misma firma que el
source de strategy.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict

from core import paths as _paths
from core.strategy_map import DEFAULT_MAP, StrategyMapError, parse_map

STRATEGY_MAP_PATH = _paths.STRATEGY_MAP_PATH

_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


# ============================================================
# Disco
# ============================================================

def _leer() -> str:
    """El texto del mapa, o None si el fichero no existe."""
    if not os.path.exists(STRATEGY_MAP_PATH):
        return None
    with open(STRATEGY_MAP_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _firma() -> tuple:
    """`(mtime_ns, tamaño)` del fichero, o None si no está. Su cambio invalida."""
    try:
        st = os.stat(STRATEGY_MAP_PATH)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


# ============================================================
# Cache
# ============================================================

def _cargada() -> bool:
    if not _cache:
        return False
    return _cache.get("firma") == _firma()


def invalidate() -> None:
    """Olvida el mapa cacheado. La próxima lectura vuelve al fichero."""
    with _cache_lock:
        _cache.clear()


# ============================================================
# Lectura
# ============================================================

def load(force: bool = False) -> Dict[int, str]:
    """El mapa magic -> perfil, cacheado.

    Fichero ausente -> `DEFAULT_MAP` (todo al perfil "default", el
    comportamiento histórico). Fichero mal formado -> `StrategyMapError`: un
    default silencioso aquí escondería un mapa que nadie pudo leer.

    Devuelve una COPIA, igual que `strategy_source.load`: un consumidor que
    edite el mapa no puede dejar la caché con un estado que el fichero nunca
    tuvo.
    """
    if not force and _cargada():
        return dict(_cache["mapa"])
    texto = _leer()
    mapa = dict(DEFAULT_MAP) if texto is None else parse_map(_safe_load(texto))
    with _cache_lock:
        _cache.clear()
        _cache["mapa"] = mapa
        _cache["firma"] = _firma()
    return dict(mapa)


def _safe_load(texto: str) -> Any:
    """`yaml.safe_load` traduciendo el fallo de sintaxis a `StrategyMapError`."""
    import yaml  # noqa: PLC0415 - tardío: solo el loader necesita PyYAML

    try:
        doc = yaml.safe_load(texto)
    except yaml.YAMLError as exc:
        raise StrategyMapError(
            "strategy_map.yaml no es un YAML válido: {0}".format(exc)
        ) from exc
    if doc is None:
        return {}
    return doc


__all__ = [
    "STRATEGY_MAP_PATH",
    "invalidate",
    "load",
]