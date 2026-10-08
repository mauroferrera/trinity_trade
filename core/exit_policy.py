"""Política de salida por perfil: quién mueve el stop y quién no (F5, D-078).

Una estrategia que entra con un TP y un SL fijos no necesita nada más: su salida
ya está definida en la apertura. El CTA Swing D1 es distinto —la convalidación de
F2 (D-073/D-074) vive de un trailing chandelier que acompaña el extremo— y esa
regla no puede quedarse dentro de `research/backtest_cta.py`: en real, el stop de
una posición abierta lo tiene que mover ALGUIEN. Este módulo es ese alguien, en
forma pura y sin tocar el bróker.

Dos piezas:

- `policy_for(perfil)`: el registro de qué perfil usa qué política. Hoy solo el
  CTA mueve stops (`chandelier`); todo lo demás es `static`, y un perfil nuevo
  tiene que entrar aquí ANTES de que su servicio mueva nada.
- `trailing_stop(...)`: el ratchet. Delega la aritmética en
  `research.cta.update_trail` —la MISMA función que corrió en el backtest de
  F2—, porque dos implementaciones de "nunca afloja" son dos formas de que una de
  ellas afloje un día sin que nada lo note. Lo que este módulo añade sobre el
  motor es el vocabulario del bróker: las posiciones llegan como BUY/SELL y el
  motor razona en long/short.

Lo que NO hace, y por qué es así:

- No coloca el stop inicial: eso es `cta.chandelier`, en la apertura (y en la
  alerta que escribe la fila).
- No inventa un stop donde no lo había: un `stop_actual` ausente o `<= 0` (el
  "sin stop" del bróker) devuelve `None`, y quien llama decide si esa posición
  se toca. Un trailing que colocara un stop donde el operador abrió sin stop
  cerraría una posición que nadie mandó cerrar.
- No mira mercado, config ni reloj: recibe números y devuelve un número. La
  pureza la vigila `tests/unit/test_core_purity.py` como el resto de `core/`.
"""

from __future__ import annotations

from typing import Optional

from research import cta

#: Salida con niveles fijos en la apertura: nadie mueve nada después.
POLICY_STATIC = "static"

#: Trailing chandelier ratcheteado con el extremo de la barra cerrada.
POLICY_CHANDELIER = "chandelier"

#: Perfiles con trailing. Solo el CTA D1, que es el único convalidado con esta
#: regla (F2). Un perfil nuevo que mueva stops sin estar declarado aquí estaría
#: aplicando una regla que nadie validó.
CHANDELIER_PROFILES = frozenset({"cta"})

#: Vocabulario del motor (`research/cta.py`): una dirección es larga si dice
#: `long` o `BUY`, y corta si dice `short` o `SELL`. Se aceptan los dos porque
#: este módulo se habla tanto con señales del motor como con filas de posiciones.
_LARGO = ("long", "BUY")
_CORTO = ("short", "SELL")


def policy_for(profile: Optional[str]) -> str:
    """`(POLICY_STATIC | POLICY_CHANDELIER)` para un perfil.

    El perfil desconocido (o ausente) resuelve `static` a propósito: mover
    stops es la decisión que no se toma por defecto. Un perfil que quiera
    trailing aparece en `CHANDELIER_PROFILES` con su convalidación detrás.
    """
    if profile and str(profile).strip().lower() in CHANDELIER_PROFILES:
        return POLICY_CHANDELIER
    return POLICY_STATIC


def _es_largo(direction: str) -> bool:
    """`True` para long/BUY, `False` para short/SELL. Cualquier otra cosa revienta."""
    d = str(direction or "").strip().upper()
    if d in tuple(x.upper() for x in _LARGO):
        return True
    if d in tuple(x.upper() for x in _CORTO):
        return False
    raise ValueError("dirección desconocida para la política de salida: "
                     "{0!r} (se espera BUY/SELL o long/short)".format(direction))


def trailing_stop(direction: str, stop_actual: Optional[float], ref: float,
                  atr_value: float, mult: float) -> Optional[float]:
    """Stop ratcheteado con el extremo `ref`, o `None` si no hay cambio que aplicar.

    `ref` es el extremo de la barra CERRADA que manda la política (high en long,
    low en short) y `atr_value` el ATR de esa misma barra: la misma pareja que
    usaba `simulate_trade` barra a barra en la convalidación. `stop_actual` es el
    stop que ya tiene la posición, y la memoria del ratchet vive EN él: no hace
    falta recorrer el histórico, porque el stop actual ya es el máximo (long) o
    el mínimo (short) de todo lo que el trailing vio.

    Devuelve `None` en dos casos, y por razones distintas:

    - no hay stop previo (`None`, `0`, negativo): nada que ratchear, y este
      módulo no coloca stops nuevos — eso es la apertura;
    - el candidato no mejora el actual: el chandelier nunca afloja, así que un
      stop que no sube (long) no se toca.

    Quien llama distingue los dos casos mirando el stop de la posición antes de
    llamar: `None` aquí solo significa "no hay nada que mandar al bróker".
    """
    largo = _es_largo(direction)
    actual: Optional[float] = float(stop_actual) if stop_actual is not None else None
    if actual is None or actual <= 0:
        return None

    nuevo = cta.update_trail(
        "long" if largo else "short", actual, float(ref), float(atr_value), float(mult))
    if actual is not None and nuevo == actual:
        return None
    return float(nuevo)


__all__ = [
    "CHANDELIER_PROFILES",
    "POLICY_CHANDELIER",
    "POLICY_STATIC",
    "policy_for",
    "trailing_stop",
]
