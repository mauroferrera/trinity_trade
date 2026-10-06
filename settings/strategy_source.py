"""La otra mitad de `strategy.yaml`: el fichero.

`core/strategy.py` sabe qué es una regla válida y cómo se aplana; este módulo sabe
dónde está el archivo, cómo se lee y cómo se escribe sin perderle los comentarios.
La separación está justificada en el docstring de `settings/__init__.py`.

Nombres públicos: los mismos que usaba REF/strategy.py, porque `store` los consume
por nombre (`get_config`, `save`, `get_data_sources`, `get_agent_topics`,
`get_watcher_config`) y ese seam es lo que evita tocar los otros 90 consumidores de
la config.

Dos diferencias respecto a REF, y las dos son correcciones:

1. **La caché se invalida sola.** REF cacheaba la config hasta que alguien guardaba
   algo, así que editar `strategy.yaml` a mano no cambiaba nada hasta reiniciar: la
   UI seguía enseñando el valor viejo y guardaba encima. Aquí la entrada de caché
   guarda la firma del fichero (mtime + tamaño), y `load()` recarga cuando no
   cuadra. El guardado sigue invalidando, por si acaso.
2. **La escritura es atómica y en LF.** REF escribía encima del archivo con
   `open(..., "w")`: un fallo a mitad dejaba el `strategy.yaml` —el fichero que
   define si se opera— truncado, y en Windows además convertía los LF a CRLF en cada
   guardado. Aquí se escribe a un temporal y se renombra con `os.replace`.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any, Dict

from core import paths as _paths
from core import strategy as _core

#: Definido aquí y no en `core.strategy` porque es lo único de este módulo que sabe
#: de ficheros: `core/` no puede ni nombrarlos. Los tests lo redirigen con
#: `monkeypatch.setattr` (igual que `store.DB_PATH`) para no escribir el YAML real.
STRATEGY_PATH = _paths.STRATEGY_PATH

_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


# ============================================================
# Disco
# ============================================================

def _leer() -> str:
    if not os.path.exists(STRATEGY_PATH):
        # Falla en claro: sin archivo no hay plan que ejecutar.
        raise _core.StrategyConfigError(
            f"No se encontró {STRATEGY_PATH}. El archivo de estrategia es obligatorio."
        )
    with open(STRATEGY_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _escribir(text: str) -> None:
    """Escribe `text` en el YAML de forma atómica y sin traducir los saltos de línea."""
    tmp = f"{STRATEGY_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, STRATEGY_PATH)


def _firma() -> tuple:
    """`(mtime_ns, tamaño)` del YAML, o `None` si no está. Su cambio invalida caché."""
    try:
        st = os.stat(STRATEGY_PATH)
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
    """Olvida la config cacheada. La próxima lectura vuelve al fichero."""
    with _cache_lock:
        _cache.clear()


# ============================================================
# Lectura
# ============================================================

def load(force: bool = False) -> Dict[str, Any]:
    """La config plana (shape histórico) de `strategy.yaml`, cacheada.

    `force=True` ignora la caché y relee el archivo aunque no haya cambiado. Lanza
    `StrategyConfigError` si el YAML falta, no es un mapeo o tiene una regla rota:
    arrancar con la mitad de las reglas sería peor que no arrancar.

    Devuelve una COPIA de la config cacheada. El guardado desde la UI edita el dict
    que le devuelve este método antes de escribirlo (es lo que hacía REF), y si
    devolviera el objeto de la caché, un guardado fallido dejaría la memoria con
    reglas que nunca llegaron al YAML: la UI enseñaría como activo un valor que solo
    existe en el proceso.
    """
    if not force and _cargada():
        return dict(_cache["flat"])

    doc = _core.load_doc(_leer())
    flat = _core.build_flat(doc)

    with _cache_lock:
        _cache.clear()
        _cache["flat"] = flat
        _cache["doc"] = doc
        _cache["firma"] = _firma()
    return flat


def get_trading_config() -> Dict[str, Any]:
    """Alias histórico: el nombre que usaba el monolito."""
    return load()


def get_config() -> Dict[str, Any]:
    """Nombre estable para `store`, la API y el agente."""
    return load()


def get_data_sources() -> Dict[str, bool]:
    return load().get("data_sources", {})


def get_watcher_config() -> Dict[str, Any]:
    return load().get("watcher_config", {})


def get_agent_topics() -> Dict[str, Any]:
    return load().get("agent_topics", {})


# ============================================================
# Escritura
# ============================================================

def _json_o_none(value: Any) -> Any:
    """Un JSON string de la config plana, parseado. `None` si no se puede."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def save(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Actualiza el YAML con las claves PLANAS de `updates` y devuelve la config ya
    releída.

    Preserva comentarios y formato: solo se reescriben las líneas cuyo valor cambia de
    verdad. `risk_weights`, `killzones` y `sl_distance_by_symbol` no se pueden expresar
    como escalar en línea, así que si cambian se regenera el archivo entero con
    `yaml.dump` — y se pierden los comentarios, como ya pasaba en REF.

    Dos correcciones sobre REF:

    - "Cambia de verdad" se mide contra la config **efectiva** (`build_flat` del
      documento), no contra la clave cruda del YAML. REF comparaba contra el
      documento, así que el primer guardado desde la UI sobre un YAML sin
      `score.weights` (o sin `killzones`) volcaba el archivo entero: los defaults que
      la propia carga había puesto en memoria llegaban al disco como si los hubiera
      escrito el usuario, y con ellos se perdían todos los comentarios.
    - Se valida ANTES de escribir. REF escribía y luego recargaba, así que un valor
      fuera de rango dejaba el `strategy.yaml` —el fichero que decide si se opera—
      ilegible para el siguiente arranque, y devolvía un 400 como si el problema fuera
      de la petición.
    """
    updates = dict(updates or {})
    doc = _core.load_doc(_leer())
    with open(STRATEGY_PATH, "r", encoding="utf-8") as fh:
        text = fh.read()
    vigente = _core.build_flat(doc)

    # Vista previa en memoria: exactamente lo que se escribiría, validado. Si algo no
    # cuadra, se dice sin haber tocado el disco.
    _core.build_flat(_core.apply_updates(copy.deepcopy(doc), updates))

    changed = False
    needs_full_dump = False
    for key, value in updates.items():
        mapping = _core.FLAT_TO_YAML.get(key)
        if not mapping:
            continue
        if key in ("risk_weights", "killzones", "sl_distance_by_symbol"):
            actual = vigente[key]
            if isinstance(actual, str):
                actual = json.loads(actual)
            if key == "sl_distance_by_symbol":
                if dict(value or {}) == dict(actual or {}):
                    continue
            elif actual == _json_o_none(value):
                continue
            needs_full_dump = True
            continue

        section, subkey = mapping
        if section is None or subkey is None:
            continue
        # Una clave que ya vale lo mismo no se reescribe: el parche conservaría el
        # comentario inline pero recolocaría su alineación, y un guardado que no
        # cambia nada no debería ensuciar el diff ni el mtime del archivo.
        if vigente.get(key) == value:
            continue
        new_text = _core.patch_text(text, section, subkey, value)
        if new_text != text:
            text = new_text
            changed = True

    if needs_full_dump:
        doc = _core.apply_updates(doc, updates)
        text = _core.dump_yaml(doc)
        changed = True

    if changed:
        _escribir(text)

    invalidate()
    return load()


__all__ = [
    "STRATEGY_PATH",
    "get_agent_topics",
    "get_config",
    "get_data_sources",
    "get_trading_config",
    "get_watcher_config",
    "invalidate",
    "load",
    "save",
]