"""El mapa de estrategias: magic con el que sale una orden -> perfil de reglas.

Qué es un perfil
----------------
Un perfil es un `strategy<perfil>.yaml`: el `strategy.yaml` histórico es el perfil
"default". Cada estrategia (ILOF, CTA Swing, Mean Reversion, Copilot) tendrá su
propio YAML, su proprio magic y sus propias reglas, y todas pasarán por la MISMA
`ExecutionService` y el MISMO `risk_engine` (hoja de ruta D-071). Sin el mapa no
hay forma de saber qué reglas aplican a una orden: el magic es el único hecho que
viaja en el deal, así que es la llave del mapa.

Por qué el mapa es un fichero aparte
------------------------------------
`strategy.yaml` es UN perfil. Meter aquí los magics de los demás mezclaría en un
mismo archivo dos clases de cosas distintas, y ya se sabe lo que pasa con eso en
este repo: la distancia de SL del CTA acabaría cayendo en la del ILOF
(sl_distance_by_symbol -> by_profile_by_symbol evita exactamente ese contagio).

Lo que este módulo NO hace
--------------------------
Lee `strategy_map.yaml` (eso es `settings.strategy_map_source`, el espejo de
`strategy_source`); aquí solo valida y resuelve. Y NO toca la ejecución: F1
construye la infraestructura con tests; el primer consumidor en producción
llegará con los F2+ (D-071).

Cómo se usa
-----------
`parse_map` valida un documento del YAML; `profile_for_magic` resuelve el perfil
de un magic (desconocido -> "default", que es lo que preserva el comportamiento
histórico de un solo YAML); `sl_distance_by_profile` resuelve la distancia de SL
por perfil, con `by_profile_by_symbol` mandando sobre el legacy
`sl_distance_by_symbol` del perfil default.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core import strategy


class StrategyMapError(ValueError):
    """`strategy_map.yaml` no es utilizable: no es un mapeo o tiene un magic roto.

    Hereda de `ValueError` por lo mismo que `StrategyConfigError`: quien ya captura
    el error de config puede seguir capturándolo sin saber que esta excepción existe.
    """


#: El perfil que manda cuando algo no está en el mapa. Es el `strategy.yaml`
#: histórico, la configuración que ya está operando: desconocido o ausente en el
#: mapa TIENE que significar "el único perfil que existía", no un perfil nuevo.
PROFILE_DEFAULT = "default"

#: El magic histórico de `strategy.yaml` (`execution.magic`). Es la semilla del
#: mapa: un fichero sin esta entrada no serve para distinguir al ILOF de nada.
MAGIC_DEFAULT = 8882026

#: El mapa de fábrica: sin `strategy_map.yaml` configurado, todo es el default.
DEFAULT_MAP = {MAGIC_DEFAULT: PROFILE_DEFAULT}


def _coacciona_magic(valor: Any) -> Optional[int]:
    """Un magic como int, o None si no es un entero utilizable.

    Un flotante con decimales NO es un magic: truncarlo enmascararía un deal (o
    una entrada del mapa) que jamás encontrará su perfil. `8882026.5` no es
    "casi 8882026"; es un magic que no existe.
    """
    if isinstance(valor, bool):
        return None
    if isinstance(valor, float):
        if not valor.is_integer():
            return None
        return int(valor)
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str):
        try:
            return int(valor.strip())
        except (TypeError, ValueError):
            return None
    return None


def parse_map(raw: Any) -> Dict[int, str]:
    """Valida y normaliza el documento del mapa (un mapeo magic -> perfil).

    Acepta el documento tal cual sale de `yaml.safe_load`: el objeto entero
    (`{"map": {8882026: "default"}}`) o el mapeo pelado
    (`{8882026: "default"}`), que es lo natural en un test.

    Reglas, y por qué son duras:
    - El magic tiene que ser un ENTERO. Un magic decimal o en string que no se
      convierta es un deal que nunca va a encontrar su perfil: que reviente aquí,
      en el parseo, y no al agrupar operaciones.
    - El perfil no puede estar vacío. "default" es el heredero por defecto; un
      perfil con nombre vacío sería un segundo default y dos defaults no se
      distinguen.
    - Un documento sin `map` y sin mapeo directo es un fallo, no un default:
      un mapa vacío en silencio haría que cualquier magic nuevo cayera en
      "default" y el ILOF heredara las reglas de otro sin que nadie lo note.
    """
    doc = raw
    if isinstance(doc, dict):
        if "map" in doc:
            doc = doc.get("map")
        if isinstance(doc, dict) and not doc:
            raise StrategyMapError(
                "strategy_map.yaml: 'map' no puede estar vacío: sin entradas no "
                "hay forma de distinguir una estrategia de otra."
            )
    if not isinstance(doc, dict):
        raise StrategyMapError(
            "strategy_map.yaml: debe ser un mapeo {{magic: perfil}} o tener la "
            "clave 'map' con ese mapeo."
        )
    salida: Dict[int, str] = {}
    for magic, perfil in doc.items():
        mi = _coacciona_magic(magic)
        if mi is None or mi <= 0:
            raise StrategyMapError(
                "strategy_map.yaml: cada magic debe ser un entero > 0 (es {0}).".format(magic)
            )
        if not isinstance(perfil, str) or not perfil.strip():
            raise StrategyMapError(
                "strategy_map.yaml: el perfil de {0} debe ser un texto no vacío.".format(magic)
            )
        salida[mi] = perfil.strip()
    return salida


def profile_for_magic(magic: Any, mapa: Optional[Dict[int, str]] = None) -> str:
    """El perfil de un magic, o `PROFILE_DEFAULT` si no está en el mapa.

    Un magic desconocido cae SIEMPRE en el default: es el heredero histórico, el
    `strategy.yaml` que ya está operando. Hacer que un magic nuevo levantara o
    operara con reglas vacías sería inventarse un comportamiento para algo que
    todavía no existe en el mapa.
    """
    mi = _coacciona_magic(magic)
    if mi is None:
        return PROFILE_DEFAULT
    mapa_efectivo = mapa if isinstance(mapa, dict) else DEFAULT_MAP
    return str(mapa_efectivo.get(mi, PROFILE_DEFAULT))


def magics_for(perfil: str, mapa: Optional[Dict[int, str]] = None) -> List[int]:
    """Los magics que apuntan a `perfil`, ordenados. Vacío si ese perfil no está."""
    if not perfil:
        return []
    mapa_efectivo = mapa if isinstance(mapa, dict) else DEFAULT_MAP
    return sorted(m for m, p in mapa_efectivo.items() if p == perfil)


def _positivo(value: Any) -> Optional[float]:
    """El valor como float si es un número > 0; `None` si no o si es <= 0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def sl_distance_by_profile(
    cfg: Optional[Dict[str, Any]],
    magic: Any,
    symbol: Optional[str],
    spec: Any,
    mapa: Optional[Dict[int, str]] = None,
) -> Tuple[Optional[float], str]:
    """`(distancia, origen)` de la distancia de SL, resuelta POR PERFIL.

    Orden de precedencia:

    1. `by_profile_by_symbol[perfil][SÍMBOLO]` — el mapa nuevo: la distancia del
       símbolo DENTRO del perfil al que pertenece la orden. Es lo que evita el
       contagio: un símbolo del CTA en `by_profile_by_symbol` no puede contaminar
       al ILOF ni al revés, porque lleva el perfil delante.
    2. Para el perfil "default", el `sl_distance_by_symbol` legacy (la distancia
       del `strategy.yaml` histórico). El default es el heredero y no se le
       cambia la precedencia: romperla cambiaría el comportamiento de la
       estrategia que ya está operando.
    3. `sl_distance` global.
    4. `sl_default_pips` legacy x el pip del spec.

    Quien consume devuelve el origen a la UI: "1" y "2" no se distinguen en la
    pantalla de otra forma, y un 0.0012 en oro y en EURUSD no son la misma nota.
    """
    c = dict(cfg or {})
    perfil = profile_for_magic(magic, mapa)

    slot = strategy._json_o_estructura(c.get("by_profile_by_symbol"))
    if isinstance(slot, dict):
        por_perfil = slot.get(perfil)
        if isinstance(por_perfil, dict) and symbol:
            dist = _positivo(por_perfil.get(str(symbol).strip().upper()))
            if dist is not None:
                return dist, "profile_symbol"

    if perfil == PROFILE_DEFAULT:
        return strategy.sl_distance_for(c, symbol, spec)

    dist = _positivo(c.get("sl_distance"))
    if dist is not None:
        return dist, "global"
    legacy = _positivo(c.get(strategy.LEGACY_SL_PIPS_KEY))
    pip = getattr(spec, "pip", None) if spec is not None else None
    if legacy is not None and pip:
        try:
            return legacy * float(pip), "legacy_pips"
        except (TypeError, ValueError):
            return None, "none"
    return None, "none"


__all__ = [
    "DEFAULT_MAP",
    "MAGIC_DEFAULT",
    "PROFILE_DEFAULT",
    "StrategyMapError",
    "magics_for",
    "parse_map",
    "profile_for_magic",
    "sl_distance_by_profile",
]