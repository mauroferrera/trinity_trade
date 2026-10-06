"""Calidad de ejecución: lo que la decisión NO controla y el dinero sí paga.

Módulo PURO, misma disciplina que `core.risk_engine` y `core.setup_gate`: sin
MT5, sin app, sin store, sin red. Recibe precios ya resueltos y devuelve números,
para poder probarlo entero sin terminal.

**El hueco que tapa.** El gate de entrada (`risk_engine.validate_entry`) valida
el R:R sobre los precios de la orden, y esos precios ya son `ask`/`bid`: el
spread está *dentro* de la entrada, así que el R:R sale bien. Pero hay dos
factores que ni el score ni el R:R miran, y los dos se pagan en dinero:

1. **La deriva de la entrada.** El gate justifica el trade en un NIVEL (el
   borde de un FVG, el cierre de un order block): "compro aquí porque hay
   descuento". Una orden a mercado no entra en ese nivel, entra al precio del
   instante. Si entre el gate y el envío el precio se ha movido, la tesis se ha
   desplazado: en un BUY, entrar por encima del nivel que justificaba la
   compra es exactamente lo contrario de lo que se aprobó. Con una deriva de la
   mitad del stop, el nivel que motivó la orden ya está medio stop más lejos, y
   el R:R sobre el papel sigue siendo 2.0.

2. **La distancia a la invalidez.** `validate_entry` comprueba si el precio YA
   cruzó el nivel de invalidez, pero no si queda a un centímetro de él. Una
   entrada con el 90 % de su recorrido antes de que la estructura muera no es
   una entrada con buen ratio: es una entrada con casi todo el riesgo concentrado
   en el primer tick.

Los dos son diferencias entre dos números que el sistema YA tenía y no miraba:
el precio que el gate planeó y el nivel de invalidez. Este módulo los mide, y el
servicio de ejecución los usa como puerta antes de llamar a `order_send`.

**Lo que NO hace**: no puntúa, no toca el score, no decide el lote y no predice.
Solo dice si las condiciones de llenado invalidan lo que el gate aprobó, y lo
dice con la medida a la vista para que el motivo sea auditable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Umbrales por defecto (se sobrescriben desde strategy.yaml / trading config)
# ---------------------------------------------------------------------------

# La deriva de la entrada no puede superar esta fracción de la distancia de SL.
# 0.5 = "el nivel que justificaba el trade puede estar como mucho medio stop más
# lejos". Es la lectura correcta del riesgo: a partir de ahí, lo que se está
# comprando no es el setup que se aprobó.
DEFAULT_MAX_SLIPPAGE_SL_SHARE = 0.5

# El spread no puede superar esta fracción de la distancia de SL. 0.25 = un
# cuarto del stop se va en la entrada antes de que el trade pueda trabajar.
DEFAULT_MAX_SPREAD_SHARE_OF_SL = 0.25

# La entrada debe quedar al menos a esta fracción del SL del nivel de
# invalidez. 0.25 = al menos un cuarto del recorrido antes de que la estructura
# declare muerto el setup. Por debajo, el riesgo está casi entero en el primer
# tick aunque el R:R sobre el papel sea perfecto.
DEFAULT_MIN_INVALIDATION_SL_SHARE = 0.25

# Tolerancia de las COMPARACIONES con umbral, no de las medidas. Existe porque las
# fracciones salen de restas y divisiones de coma flotante: media distancia de SL
# dividida por la distancia de SL da 0.500000000000167, no 0.5. Sin esta tolerancia
# un setup exactamente en el límite se rechaza o se acepta según el último bit, y
# un gate cuyo resultado en el borde no se puede reproducir es peor que no tener
# gate. Es 1e-9: cuatro órdenes de magnitud por debajo de cualquier diferencia que
# una persona pueda decidir, y muy por encima del ruido de la aritmética.
EPSILON = 1e-9

DIRECTION_LONG = "BUY"
DIRECTION_SHORT = "SELL"

# Códigos de motivo. Se exportan porque quien los LEA (el watcher los vuelve a
# expresar en pips, la auditoría los cuenta por tipo) tiene que poder distinguir
# las reglas sin parsear prosa: un motivo es texto para el operador y un código
# para la máquina, y el texto puede cambiar sin romper el conteo.
REASON_INVALIDEZ = "INVALIDEZ_ENTRADA"
REASON_DERIVA = "DERIVA_ENTRADA"
REASON_SPREAD = "SPREAD_sobre_SL"


def reason_code(reason: str) -> str:
    """Código de un motivo: 'SPREAD_sobre_SL: ...' -> 'SPREAD_sobre_SL'."""
    return str(reason or "").split(":", 1)[0].strip()


def _num(value: Any) -> Optional[float]:
    """float() o None, y NUNCA 0.0 en lugar de un hueco.

    El 0 SÍ es un número medido —un spread de 0 puntos, una deriva de 0 pips— y
    por eso se conserva: confundirlo con "no medido" haría que una entrada
    exactamente en el nivel planificado pareciera un dato que falta. Lo que sí
    es un hueco es None, un texto no numérico, o un bool (que en Python es un
    int y colaría como 0 o 1 sin querer).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _sign(direction: str) -> Optional[int]:
    d = str(direction or "").strip().upper()
    if d == DIRECTION_LONG:
        return 1
    if d == DIRECTION_SHORT:
        return -1
    return None


def spread_points(bid: Optional[float], ask: Optional[float],
                  point: Optional[float]) -> Optional[float]:
    """ask - bid en puntos del símbolo. None si falta algo o si la cita no cierra."""
    b, a, p = _num(bid), _num(ask), _num(point)
    if b is None or a is None or p is None or p <= 0:
        return None
    if not (b > 0 and a > 0):
        return None
    spread = (a - b) / p
    if spread < 0:
        return None
    return round(spread, 2)


def measure(entry: Optional[float],
            sl: Optional[float],
            tp: Optional[float],
            direction: str,
            point: Optional[float],
            planned_entry: Optional[float] = None,
            sl_distance: Optional[float] = None,
            invalidate_level: Optional[float] = None,
            bid: Optional[float] = None,
            ask: Optional[float] = None) -> Dict[str, Any]:
    """Mide las condiciones de llenado. NUNCA lanza y NUNCA bloquea.

    Devuelve siempre el mismo juego de claves, con None donde no se pudo medir.
    Que no pueda medir es un resultado legítimo: quien llama decide qué hacer con
    un hueco, y este módulo no decide por él.

    **La distancia de referencia.** Las tres reglas se miden como fracción de una
    distancia de SL, y esa fracción tiene UNA sola pregunta que responder: ¿cuánta
    parte del riesgo presupuestado se ha ido en las condiciones de llenado? Por eso
    el denominador es la distancia PLANEADA (`sl_distance`), no la que sale de
    restar los precios redondeados.

    No es una sutileza, es la diferencia entre una regla y una letra muerta. El
    ejecutor recalcula el SL desde el precio de llenado, así que si el
    denominador fuera ese, una deriva de medio stop daría
    `medio/(1+medio) = 0.33` y la deriva solo superaría un umbral de 0.5 al
    moverse un stop ENTERO — cuando la constante dice, en sus propias palabras,
    "medio stop más lejos". Además el lote se dimensiona con la distancia
    planificada, de modo que el denominado planificado es también el del riesgo
    real. Sin distancia planeada se cae a la geometría efectiva, que es lo único
    que hay, y `ref_sl_distance` deja constancia de cuál se usó.
    """
    e, s, t = _num(entry), _num(sl), _num(tp)
    pt = _num(point)
    d = _sign(direction)
    # La dirección se normaliza UNA vez y se usa para los dos trabajos: la que
    # calcula el signo de la deriva y la que se graba. Si se normalizaran por
    # separado, un " buy " se usaría como BUY para medir y se reportaría como
    # " BUY ", que ya no es BUY: la fila diría "dirección desconocida" en una
    # orden cuya deriva sí se midió con el signo correcto.
    norm_dir = str(direction or "").strip().upper() or None

    sl_dist = abs(e - s) if (e is not None and s is not None) else None
    tp_dist = abs(t - e) if (t is not None and e is not None) else None
    sp_points = spread_points(bid, ask, pt)
    sp_price = sp_points * pt if (sp_points is not None and pt) else None

    # Denominador de las tres fracciones: el stop que la decisión presupuesta.
    planned_dist = _num(sl_distance)
    ref_dist = planned_dist if (planned_dist and planned_dist > 0) else sl_dist

    # Deriva de la entrada. `adverse` es positiva cuando el precio se movió EN
    # CONTRA: un BUY que entra por encima de lo planificado, un SELL por debajo.
    # Es la única forma de comparar los dos lados con un mismo signo.
    planned = _num(planned_entry)
    slip_price = None
    slip_adverse = None
    if e is not None and planned is not None and d is not None:
        slip_price = e - planned
        slip_adverse = slip_price * d

    # Distancia a la invalidez, en la escala del stop. Positiva = la
    # estructura aún no ha muerto; cero o negativa = ya está muerta o lo estaba.
    inv = _num(invalidate_level)
    inv_dist = None
    inv_share = None
    if e is not None and inv is not None and d is not None:
        inv_dist = (e - inv) * d
        if ref_dist:
            inv_share = inv_dist / ref_dist

    out: Dict[str, Any] = {
        "entry": e,
        "sl": s,
        "tp": t,
        "direction": norm_dir,
        "point": pt,
        # Geometría efectiva de la orden
        "sl_distance": sl_dist,
        "tp_distance": tp_dist,
        "rr_actual": (tp_dist / sl_dist) if (tp_dist and sl_dist) else None,
        "rr_planned": None,
        # Denominador de las fracciones: planned_sl_distance si se conoce, y si
        # no la geometría efectiva. Se graba para que la fila de setup_log diga
        # contra qué se midió cada regla.
        "ref_sl_distance": ref_dist,
        "ref_is_planned": bool(planned_dist and planned_dist > 0),
        # Coste de entrada
        "spread_points": sp_points,
        "spread_price": sp_price,
        # `is not None` y no verdad: un spread de 0.0 es una MEDICIÓN (bid == ask,
        # cotización bloqueada o sesión de spread cero), no un dato ausente. Con
        # verdad, `0.0` caería en el `None` y la fila marcaría `unknown` de spread
        # en la única situación en que el spread es perfecto, que es la que menos
        # excuse tiene para dudar. El denominador sí usa verdad a propósito: una
        # distancia de referencia de 0 no divide, y eso sí es un hueco.
        "spread_share_of_sl": (sp_price / ref_dist) if (sp_price is not None and ref_dist) else None,
        "slippage_price": slip_price,
        "slippage_adverse_price": slip_adverse,
        "slippage_points": (abs(slip_adverse) / pt) if (slip_adverse is not None and pt) else None,
        "slippage_share_of_sl": (abs(slip_adverse) / ref_dist) if (slip_adverse is not None and ref_dist) else None,
        # Margen antes de que la estructura mate el setup
        "invalidation_distance": inv_dist,
        "invalidation_share_of_sl": inv_share,
    }

    # R:R planificado: el que el gate pidió por config, no el que sale de los
    # precios redondeados. La diferencia entre ambos es lo que costó el redondeo.
    dist = _num(sl_distance)
    if dist and dist > 0 and tp_dist is not None:
        out["rr_planned"] = tp_dist / dist
    return out


def evaluate(entry: Optional[float],
             sl: Optional[float],
             tp: Optional[float],
             direction: str,
             point: Optional[float],
             planned_entry: Optional[float] = None,
             sl_distance: Optional[float] = None,
             invalidate_level: Optional[float] = None,
             bid: Optional[float] = None,
             ask: Optional[float] = None,
             cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Mide y dictamina. Clave: `approved` False con `reasons` explicados.

    Las tres reglas que rechazan, en orden de importancia para el dinero:

    1. `INVALIDEZ_ENTRADA`: la entrada está más cerca del nivel que mata el
       setup que del que permite trabajar. Riesgo casi entero en el primer tick.
    2. `DERIVA_ENTRADA`: la entrada se ha ido demasiado lejos del nivel que
       justificaba el trade (fracción del SL).
    3. `SPREAD_sobre_SL`: el spread se ha comido demasiado del stop.

    Un dato que no se puede medir NO rechaza: no se bloquea una operación por
    falta de un dato. Lo que sí hace es dejar `unknown` en el veredicto, que es la
    diferencia entre "no pasó nada raro" y "no se pudo comprobar".

    **El epsilon de los umbrales.** Las fracciones se comparan con tolerancia, y
    no es cosmetismo: `abs(1.10050 - 1.10000) / 0.00100` da `0.500000000000167` en
    coma flotante, no `0.5`. Con un `>` a secas, una entrada EXACTAMENTE en el
    umbral —que la regla dice que se admite, porque dice "no puede superar"— se
    rechazaba según el redondeo del último bit. Un gate que en el borde depende del
    redondeo no es un gate: el mismo setup se acepta o se rechaza según la
    plataforma y nadie puede reproducir por qué. La tolerancia se aplica solo a
    las COMPARACIONES con umbral, que son decisiones; las medidas se devuelven tal
    cual, sin redondear, para que el log enseñe el número de verdad.
    """
    c = dict(cfg or {})
    m = measure(entry, sl, tp, direction, point,
                planned_entry=planned_entry, sl_distance=sl_distance,
                invalidate_level=invalidate_level, bid=bid, ask=ask)

    max_slip = float(c.get("max_slippage_sl_share", DEFAULT_MAX_SLIPPAGE_SL_SHARE))
    max_spread = float(c.get("max_spread_share_of_sl", DEFAULT_MAX_SPREAD_SHARE_OF_SL))
    min_inv = float(c.get("min_invalidation_sl_share", DEFAULT_MIN_INVALIDATION_SL_SHARE))

    reasons: List[str] = []
    rejected = False
    unknown: List[str] = []

    if m["direction"] not in (DIRECTION_LONG, DIRECTION_SHORT):
        unknown.append("direccion")
    if m["sl_distance"] is None or m["sl_distance"] <= 0:
        # Sin geometría no hay nada que juzgar; que lo handle validate_entry.
        m["approved"] = True
        m["verdict"] = "sin_geometria"
        m["reasons"] = []
        m["unknown"] = ["sl_distance"]
        return m

    # 1. Invalidez alcanzable antes que el stop
    share = m["invalidation_share_of_sl"]
    if share is None:
        unknown.append("invalidation_level")
    elif share < min_inv - EPSILON:
        rejected = True
        reasons.append(
            "%s: la entrada queda a %.0f%% del SL del nivel que "
            "invalida el setup (minimo %.0f%%); el riesgo se concentra en el "
            "primer tick." % (REASON_INVALIDEZ, share * 100, min_inv * 100)
        )

    # 2. Deriva de la entrada
    slip = m["slippage_share_of_sl"]
    if slip is None:
        if planned_entry is None:
            unknown.append("planned_entry")
    elif slip > max_slip + EPSILON:
        rejected = True
        if m["slippage_adverse_price"] and m["slippage_adverse_price"] > 0:
            reasons.append(
                "%s: la entrada se ha ido %.0f pips en contra "
                "(%.0f%% del SL, maximo %.0f%%); el nivel que justificaba el "
                "setup ya no es este." % (
                    REASON_DERIVA,
                    (m["slippage_adverse_price"] / m["point"]) if m["point"] else 0.0,
                    slip * 100, max_slip * 100)
            )
        else:
            reasons.append(
                "%s: la entrada se ha ido %.0f%% del SL respecto al "
                "nivel planificado (maximo %.0f%%)." % (
                    REASON_DERIVA, slip * 100, max_slip * 100)
            )

    # 3. Spread sobre el stop
    sp_share = m["spread_share_of_sl"]
    if sp_share is None:
        unknown.append("spread")
    elif sp_share > max_spread + EPSILON:
        rejected = True
        reasons.append(
            "%s: el spread es el %.0f%% de la distancia de SL "
            "(maximo %.0f%%); el stop tiene que recorrerse %.2fx para llegar a "
            "breakeven." % (REASON_SPREAD, sp_share * 100, max_spread * 100,
                            1.0 + sp_share)
        )

    m["approved"] = not rejected
    m["verdict"] = "rechazada" if rejected else (
        "sin_medir" if unknown else "ok")
    m["reasons"] = reasons
    m["unknown"] = unknown
    m["thresholds"] = {
        "max_slippage_sl_share": max_slip,
        "max_spread_share_of_sl": max_spread,
        "min_invalidation_sl_share": min_inv,
    }
    return m


__all__ = [
    "DEFAULT_MAX_SLIPPAGE_SL_SHARE",
    "DEFAULT_MAX_SPREAD_SHARE_OF_SL",
    "DEFAULT_MIN_INVALIDATION_SL_SHARE",
    "EPSILON",
    "DIRECTION_LONG",
    "DIRECTION_SHORT",
    "REASON_DERIVA",
    "REASON_INVALIDEZ",
    "REASON_SPREAD",
    "evaluate",
    "measure",
    "reason_code",
    "spread_points",
]
