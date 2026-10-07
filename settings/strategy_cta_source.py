"""La mitad de `config/strategy_cta.yaml` que toca el fichero.

Mismo reparto que `settings/strategy_source.py` y `settings/strategy_map_source.py`:
`core/` no puede abrir ficheros, y meter la lectura en `store` ataría la persistencia a
otra fuente de verdad. Este módulo sabe dónde está el perfil del CTA Swing D1 (F4,
D-077), cómo se lee y qué forma tiene que tener; el servicio
(`api/services/cta_alert_service.py`) sabe qué significa.

Tres diferencias con los otros dos sources, y las tres son decisiones:

1. **El fichero NO es obligatorio.** Ausente → `{}`, y el servicio queda
   deshabilitado CON MOTIVO. `strategy.yaml` es obligatorio porque define si se
   opera; este perfil define si una estrategia CONCRETA está desplegada, y no
   desplegarla es un estado legítimo, no un fallo de arranque.
2. **La validación vive AQUÍ, no en `core/`.** El schema de este YAML es de F4 y
   `core/` no se toca en esta fase (regla de oro); un campo con el tipo equivocado
   lanza `StrategyConfigError` (el tipo que ya distinguen store y la API) en vez de
   llegar al servicio y fallar como `KeyError` a mitad de un scan.
3. **Se devuelve una copia profunda.** A diferencia del mapa (escalar plano), el
   perfil trae dicts y listas anidados (`engine`, `symbols`): con una copia
   superficial, un consumidor que editara `perfil["engine"]` corrompería la caché
   con un estado que el fichero nunca tuvo.

Nombres públicos: `load` e `invalidate`, con la misma firma que los otros sources.
"""

from __future__ import annotations

import copy
import os
import threading
from typing import Any, Dict

from core import paths as _paths
from core.strategy import StrategyConfigError

STRATEGY_CTA_PATH = _paths.STRATEGY_CTA_PATH

_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()

#: Campos con tipo propio y su regla. El servicio valida la PRESENCIA y la
#: coherencia con el mapa; esto solo mira que cada valor sea de la forma esperada.
_CAMPOS_BOOL = ("enabled",)
_CAMPOS_ENTERO_POSITIVO = ("bars",)
_CAMPOS_TEXTO = ("comment", "timeframe")
_CAMPOS_ENGINE_ENTERO = ("atr_n", "don_n")


# ============================================================
# Disco
# ============================================================

def _leer() -> str:
    """El texto del perfil, o `None` si el fichero no existe."""
    if not os.path.exists(STRATEGY_CTA_PATH):
        return None
    with open(STRATEGY_CTA_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _firma() -> tuple:
    """`(mtime_ns, tamaño)` del fichero, o `None` si no está. Su cambio invalida."""
    try:
        st = os.stat(STRATEGY_CTA_PATH)
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
    """Olvida el perfil cacheado. La próxima lectura vuelve al fichero."""
    with _cache_lock:
        _cache.clear()


# ============================================================
# Validación del shape
# ============================================================

def _entero_positivo(valor: Any, campo: str) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        raise StrategyConfigError(
            "strategy_cta.yaml: '{0}' debe ser un entero > 0 (recibido: {1!r}).".format(
                campo, valor))
    return valor


def _parse(doc: Any) -> Dict[str, Any]:
    """El perfil validado, o `StrategyConfigError` con el campo que falló.

    Solo valida TIPOS (la forma). Que el magic exista en el mapa, que `symbols` no
    esté vacío o que el motor traiga sus tres parámetros es coherencia entre
    ficheros, y eso lo decide el servicio en cada scan.
    """
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise StrategyConfigError(
            "strategy_cta.yaml: el perfil debe ser un mapeo, no {0}.".format(
                type(doc).__name__))

    salida = dict(doc)
    for campo in _CAMPOS_BOOL:
        if campo in salida and not isinstance(salida[campo], bool):
            raise StrategyConfigError(
                "strategy_cta.yaml: '{0}' debe ser true/false (recibido: {1!r}).".format(
                    campo, salida[campo]))
    for campo in _CAMPOS_ENTERO_POSITIVO:
        if campo in salida:
            _entero_positivo(salida[campo], campo)
    for campo in _CAMPOS_TEXTO:
        if campo in salida and (not isinstance(salida[campo], str) or not salida[campo].strip()):
            raise StrategyConfigError(
                "strategy_cta.yaml: '{0}' debe ser un texto no vacío (recibido: {1!r}).".format(
                    campo, salida[campo]))
    if "magic" in salida:
        _entero_positivo(salida["magic"], "magic")
    if "symbols" in salida:
        simbolos = salida["symbols"]
        if not isinstance(simbolos, list) or any(
                not isinstance(s, str) or not s.strip() for s in simbolos):
            raise StrategyConfigError(
                "strategy_cta.yaml: 'symbols' debe ser una lista de textos no vacíos.")
    if "engine" in salida:
        engine = salida["engine"]
        if not isinstance(engine, dict):
            raise StrategyConfigError(
                "strategy_cta.yaml: 'engine' debe ser un mapeo (recibido: {0!r}).".format(
                    type(engine).__name__))
        for campo in _CAMPOS_ENGINE_ENTERO:
            if campo in engine:
                _entero_positivo(engine[campo], "engine.{0}".format(campo))
        if "mult" in engine:
            mult = engine["mult"]
            if isinstance(mult, bool) or not isinstance(mult, (int, float)) or mult <= 0:
                raise StrategyConfigError(
                    "strategy_cta.yaml: 'engine.mult' debe ser un número > 0 "
                    "(recibido: {0!r}).".format(mult))
    return salida


# ============================================================
# Lectura
# ============================================================

def load(force: bool = False) -> Dict[str, Any]:
    """El perfil del CTA, cacheado. Fichero ausente → `{}` (perfil vacío).

    Un YAML mal formado o con un campo de otro tipo lanza `StrategyConfigError`:
    un perfil inválido tiene que sonar en el arranque del scan, no convertirse en
    una estrategia que corre con lo que hubiera.
    """
    if not force and _cargada():
        return copy.deepcopy(_cache["perfil"])
    texto = _leer()
    perfil = {} if texto is None else _parse(_safe_load(texto))
    with _cache_lock:
        _cache.clear()
        _cache["perfil"] = perfil
        _cache["firma"] = _firma()
    return copy.deepcopy(perfil)


def _safe_load(texto: str) -> Any:
    """`yaml.safe_load` traduciendo el fallo de sintaxis a `StrategyConfigError`."""
    import yaml  # noqa: PLC0415 - tardío: solo el loader necesita PyYAML

    try:
        doc = yaml.safe_load(texto)
    except yaml.YAMLError as exc:
        raise StrategyConfigError(
            "strategy_cta.yaml no es un YAML válido: {0}".format(exc)
        ) from exc
    if doc is None:
        return {}
    return doc


__all__ = [
    "STRATEGY_CTA_PATH",
    "invalidate",
    "load",
]
