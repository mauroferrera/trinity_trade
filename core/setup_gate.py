"""La puerta del setup: zona de entrada, niveles y veredicto de aprobación.

Modulo PURO, como el resto de `core/`: no lee ficheros, no habla con el bróker y no
importa `adapters`. Recibe el snapshot ya armado y la config ya aplanada, y devuelve
el plan (dirección, entry, SL, TP) con la razón exacta de cada rechazo.

Por qué vive aquí y no en un `watcher.py`
------------------------------------------
En REF esta lógica estaba en `watcher.py`, que además era el proceso que escaneaba
símbolos en bucle, guardaba deduplicaciones y mandaba órdenes. Al separar el
cálculo del ciclo, la mitad que decide **si un setup es operable** queda testeable
sin terminal, sin base de datos y sin reloj real, y el bucle se puede portar más
tarde sin arrastrar la aritmética con él.

Lo que este módulo NO hace
--------------------------
- No avisa de calidad de ejecución. REF tenía aquí un bloque
  (`execution_quality.evaluate`) que medía spread y distancia a la invalidez como
  AVISO, sin rechazar. Ese módulo no está portado, así que aquí no hay ni el
  bloque ni una lista de avisos vacía que sugiera que se midió algo: una clave
  `warnings` siempre vacía es indistinguishable de "se midió y no hay nada que
  decir".
- No conoce el `SymbolSpec`. Cuando hace falta, lo usa por `getattr` (lo mismo que
  `core.strategy.sl_distance_for`): la dependencia va de `adapters` hacia `core`, y
  el núcleo no puede importar al adaptador ni para leer unos dígitos.
- No sabe de noticias ni de límites de cuenta. Eso lo compone quien llama, porque
  son fuentes que llegan por puertos distintos.

La killzone es un AND duro, no un factor de score
-------------------------------------------------
`score >= min_score and in_killzone and ...` — tal cual lo hacía REF, y por el
motivo que explica `risk_engine.DEFAULT_WEIGHTS["killzone"] = 0.0`: la ventana es
acceso, no puntuación. Si esto devolviera `approved=True` fuera de la ventana, la
evaluación estaría midiendo más de lo que el sistema ejecuta.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core import risk_engine, strategy

#: R:R por defecto cuando la config no declara `tp_ratio_r`. Es el 2.0 de REF: con un
#: valor accidentalmente nulo el TP caería en la propia entrada y el "plan" sería una
#: orden con objetivo en el sitio de donde ya está el precio.
DEFAULT_TP_RATIO = 2.0

#: Múltiplo de la distancia de SL que se admite entre el precio y la zona de entrada.
#: Dimensionless a propósito: un absoluto en precio (los 0.01 de REF) son 12 pips en
#: EURUSD, pero en oro son medio dólar y en el Nasdaq medio punto, así que el mismo
#: número filtraba en un mercado y se desactivaba en el otro sin decir nada.
ENTRY_ZONE_MAX_SPAN_SL_MULT = 8.0

#: Fallback ABSOLUTO en precio, solo cuando no se conoce ninguna distancia de SL.
#: No es una calibración: es el umbral histórico, para no inventar uno.
ENTRY_ZONE_MAX_SPAN = 0.01

#: Tolerancia al comparar el span con su límite. Sin ella, una zona exactamente en el
#: borde se rechaza por error de representación binaria: `1.13910 - 0.01` reconstruye
#: `0.010000000000000009`, que es mayor que 0.01. El límite es "más de 100 pips", no
#: "100 pips y un ulp".
SPAN_EPS = 1e-12

#: No hay ninguna zona del lado correcto de la dirección: el gate cae a entrada a
#: mercado, que es el comportamiento histórico.
ZONE_NO_AXIS = "SIN_ENTRADA_EN_EJE"

#: Hay zonas del lado correcto pero todas están demasiado lejos: el setup se RECHAZA
#: en vez de convertirse en una entrada a mercado disfrazada.
ZONE_TOO_FAR = "ZONA_ENTRADA_MUY_LEJANA"


def _positive(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f > 0 else default


def max_zone_span(cfg: Optional[Dict[str, Any]] = None,
                  sl_distance: Optional[float] = None) -> float:
    """Distancia máxima precio -> zona de entrada, en unidades de precio.

    Múltiplo de la distancia de SL (portable entre mercados) y, si no se conoce
    ninguna, el absoluto histórico. `cfg["zone_max_span_sl_mult"]` deja ajustarlo sin
    tocar código.
    """
    mult = _positive((cfg or {}).get("zone_max_span_sl_mult"), ENTRY_ZONE_MAX_SPAN_SL_MULT)
    dist = _positive(sl_distance)
    if dist is not None:
        return dist * float(mult or ENTRY_ZONE_MAX_SPAN_SL_MULT)
    return float(ENTRY_ZONE_MAX_SPAN)


def entry_zone(patterns: Optional[Dict[str, Any]],
               price: Optional[float],
               direction: str,
               max_span: float = ENTRY_ZONE_MAX_SPAN) -> Dict[str, Any]:
    """Elige la zona (FVG/OB) más cercana donde entrar y explica si no hay.

    - BUY  -> la zona que queda POR DEBAJO del precio (rebote alcista).
    - SELL -> la zona que queda POR ENCIMA (rechazo bajista).

    Devuelve `{"zone": dict|None, "reason": str, "span": float|None}`, y la zona
    trae el borde que mira al precio: `{"price", "kind", "top", "bottom"}`.

    `max_span` descarta las zonas más lejos del precio que ese umbral. Sin esa cota,
    una zona antigua y lejana (la ventana de 300 velas, o la cinta sintética con su
    `base_price` fijo) se convierte en una entrada a la que el mercado no va a ir:
    una orden impracticable que el gate daba por buena.

    Motivos cuando no hay zona:
      - `ZONE_NO_AXIS`: no existe ninguna del lado correcto. Una zona del lado
        CONTRARIO cuenta como `ZONE_NO_AXIS` aunque esté a miles de pips: para un
        SELL, un order block por debajo del precio no es una entrada de venta.
      - `ZONE_TOO_FAR`: las hay del lado correcto, pero todas superan `max_span`.
    """
    out: Dict[str, Any] = {"zone": None, "reason": ZONE_NO_AXIS, "span": None}
    if not patterns or price is None:
        return out

    zonas: List[Dict[str, Any]] = []
    for z in (patterns.get("fvgs") or []):
        if not isinstance(z, dict):
            continue
        copia = dict(z)
        copia["kind"] = z.get("type") or "FVG"
        zonas.append(copia)
    for z in (patterns.get("order_blocks") or []):
        if not isinstance(z, dict):
            continue
        copia = dict(z)
        copia["kind"] = z.get("type") or "OB"
        zonas.append(copia)
    zonas = [z for z in zonas if _num(z.get("bottom")) is not None and _num(z.get("top")) is not None]
    if not zonas:
        return out

    if str(direction).upper() == "BUY":
        cands = [z for z in zonas if _num(z["bottom"]) < price]
        if not cands:
            return out
        z = max(cands, key=lambda z: float(z["bottom"]))
        span = float(price) - float(z["bottom"])
    else:
        cands = [z for z in zonas if _num(z["top"]) > price]
        if not cands:
            return out
        z = min(cands, key=lambda z: float(z["top"]))
        span = float(z["top"]) - float(price)

    if span > float(max_span) + SPAN_EPS:
        out["reason"] = ZONE_TOO_FAR
        out["span"] = span
        return out

    out["zone"] = {
        "price": z["bottom"] if str(direction).upper() == "BUY" else z["top"],
        "kind": z["kind"],
        "top": z["top"],
        "bottom": z["bottom"],
    }
    out["reason"] = ""
    # El span tambien viaja cuando hay zona: es la distancia entre el precio y el
    # borde de entrada, y sin ella quien recibe el plan ve `zone_span_limit` pero
    # no puede saber cuanto margen le queda nionder por que el plan es practico.
    out["span"] = span
    return out


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def plan_levels(entry: Optional[float],
                direction: str,
                sl_distance: Optional[float],
                tp_ratio: float = 2.0,
                digits: int = 6) -> Dict[str, Optional[float]]:
    """SL/TP desde la distancia ABSOLUTA de precio. Función pura, sin spec.

    Es la versión para los caminos donde la cotización aún no se conoce (sin
    terminal, o con un puerto que devuelve el spec como dict). Si no hay entry o la
    distancia no es positiva devuelve `{"sl": None, "tp": None}` y quien llame
    rechaza con un motivo explícito: inventar un stop con un pip supuesto es, en oro,
    un stop cien veces más corto de lo que el usuario cree.

    `digits` acota el redondeo porque un nivel que no cuadra con el tick del símbolo
    no es enviable: el bróker lo rechaza o lo ajusta.
    """
    entry_f, dist = _num(entry), _num(sl_distance)
    if entry_f is None or dist is None or dist <= 0:
        return {"sl": None, "tp": None}
    ratio = _num(tp_ratio) or 0.0
    if str(direction).upper() == "BUY":
        sl, tp = entry_f - dist, entry_f + dist * ratio
    else:
        sl, tp = entry_f + dist, entry_f - dist * ratio
    nd = max(0, min(8, int(digits or 0)))
    return {"sl": round(sl, nd), "tp": round(tp, nd)}


def killzone_forced_now(killzones: Optional[List[Dict[str, str]]] = None,
                        now: Optional[datetime] = None) -> Optional[datetime]:
    """Un instante dentro de la PRIMERA ventana configurada, para el modo test.

    Usa solo las ventanas reales de la configuración (nada de horas escritas a mano):
    `killzone=1` tiene que poner el score dentro de una sesión que el operador
    reconoce, o el flag de test no estaría probando lo que dice que prueba.

    Se ancla al DÍA de `now` y a `inicio + 30 min` para no caer en el borde exacto de
    la ventana, que es donde un reloj parado da un resultado distinto del que da
    uno que avanza. `None` si no hay ninguna ventana parseable, y entonces el
    llamante cae al comportamiento normal sin haber forzado nada.
    """
    base = now if now is not None else datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    for w in (killzones or []):
        hhmm = str(w.get("start") or "").strip()
        if ":" not in hhmm:
            continue
        try:
            hora, minuto = (int(x) for x in hhmm.split(":")[:2])
        except (TypeError, ValueError):
            continue
        if 0 <= hora <= 23 and 0 <= minuto <= 59:
            dia = base.replace(hour=hora, minute=minuto, second=0, microsecond=0)
            return dia + timedelta(minutes=30)
    return None


def _spec_attr(spec: Any, nombre: str, default: Any = None) -> Any:
    """Lee un atributo del `SymbolSpec` sin importarlo (lo da `adapters`)."""
    return getattr(spec, nombre, default) if spec is not None else default


def _spec_distance(spec: Any, distancia: float) -> str:
    """Presenta una distancia en la unidad del símbolo, o en precio si no hay spec.

    Sin spec, degradar a distancia absoluta es lo honesto: decir "0.0 pips" en el oro
    es un número que parece medido y no lo está.
    """
    formatea = getattr(spec, "format_units", None)
    if callable(formatea):
        try:
            return str(formatea(distancia))
        except Exception:  # noqa: BLE001 - presentar nunca tumba el veredicto
            pass
    return "{0:g} en precio".format(distancia)


def evaluate_gate(snapshot: Dict[str, Any],
                  cfg: Optional[Dict[str, Any]] = None,
                  spec: Any = None,
                  min_score: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Dirección, score, killzone, plan entry/SL/TP y el veredicto de aprobación.

    `spec` es el `SymbolSpec` del símbolo y es OPCIONAL: solo afecta a la presentación
    (la etiqueta "pips"/"puntos" de los motivos) y al redondeo de los niveles a los
    dígitos del símbolo. El STOP nunca depende de él — sale de
    `cfg["sl_distance"]` / `sl_distance_by_symbol`, que es donde el usuario configura
    la distancia—. Que el stop dependa del spec es el error que hizo que REF
    calibrara el oro con el pip de un par de divisas.

    `min_score` sobrescribe el umbral de la configuración SOLO si se pasa; `None`
    significa "usa `strategy.min_score_for(cfg)`". Quien llama lo declara después en
    `min_score_used`, porque un umbral distinto del de `strategy.yaml` es un umbral
    distinto y no puede quedar solo en la petición.

    Devuelve `None` si el snapshot no trae scores: no hay nada que evaluar, y un
    veredicto sobre datos que no existen sería inventado.
    """
    snap = snapshot or {}
    re_ = snap.get("risk_engine") or {}
    lados = [(d, s) for d, s in (("BUY", re_.get("bull")), ("SELL", re_.get("bear"))) if s]
    if not lados:
        return None

    direction, best = max(lados, key=lambda s: s[1].get("score") or 0)
    score = best.get("score") or 0
    verdict = best.get("verdict") or ""
    kz = (best.get("components") or {}).get("killzone") or {}
    in_killzone = bool(kz.get("in_killzone"))

    c = dict(cfg or {})
    if min_score is None:
        umbral = strategy.min_score_for(c)
    else:
        umbral = _num(min_score)
        if umbral is None:
            umbral = strategy.min_score_for(c)

    simbolo = str(snap.get("symbol") or "").upper()
    actual = _num(snap.get("current_price"))
    patterns = ((snap.get("analysis") or {}).get("patterns")) or {}
    invalidation = _num((re_.get("invalidation") or {}).get(direction))

    sl_dist, sl_origen = strategy.sl_distance_for(c, simbolo or None, spec)
    # El 2.0 es el `DEFAULT_TP_RATIO` de REF: sin `tp_ratio_r` en la config el TP cae
    # en el propio precio de entrada (R:R 1:0), y un plan con TP == entrada no es un
    # plan conservative, es uno que nunca alcanza su objetivo.
    tp_r = _num(c.get("tp_ratio_r"))
    if tp_r is None:
        tp_r = DEFAULT_TP_RATIO
    span_limit = max_zone_span(c, sl_dist)

    zona_res = entry_zone(patterns, actual, direction, span_limit) if actual is not None else None
    zona = (zona_res or {}).get("zone")
    entry = _num(zona["price"]) if zona else actual
    entry_kind = zona["kind"] if zona else "market"
    zona_lejos = bool(zona_res and zona_res.get("reason") == ZONE_TOO_FAR)

    if sl_dist is None or entry is None:
        sl, tp = None, None
    else:
        digits = _spec_attr(spec, "digits", 6) or 6
        niveles = plan_levels(entry, direction, sl_dist, tp_r, digits=int(digits))
        sl, tp = niveles["sl"], niveles["tp"]

    val: Optional[Dict[str, Any]] = None
    try:
        val = risk_engine.validate_entry(
            entry=entry, sl=sl, target=tp, direction=direction,
            current_price=actual, invalidate_level=invalidation, cfg=c,
        )
    except Exception as exc:  # noqa: BLE001 - un fallo del motor es un rechazo, no un 500
        val = {"approved": False, "rejected": True, "reasons": ["risk_engine error: {0}".format(exc)]}

    reasons: List[str] = []
    approved = bool(score >= umbral and in_killzone and not zona_lejos
                    and sl is not None and val and val.get("approved"))
    if score < umbral:
        reasons.append("Score {0:.1f} < umbral {1:.0f}.".format(score, umbral))
    if not in_killzone:
        reasons.append("Fuera de killzone.")
    if sl_dist is None:
        clave = "sl_distance_by_symbol.{0}".format(simbolo) if simbolo else "sl_distance"
        reasons.append(
            "Sin distancia de SL configurada para este símbolo ('{0}'): no se puede "
            "planificar un stop con un número inventado.".format(clave)
        )
    if zona_lejos:
        span = _num((zona_res or {}).get("span")) or 0.0
        reasons.append(
            "Zona de entrada a {0} del precio (max {1}): nivel inalcanzable en la "
            "vigencia de la orden, se descarta.".format(
                _spec_distance(spec, span), _spec_distance(spec, span_limit)
            )
        )
    if val and val.get("reasons"):
        reasons.extend(val["reasons"])

    return {
        "direction": direction,
        "score": score,
        "verdict": verdict,
        "min_score_used": umbral,
        "in_killzone": in_killzone,
        "killzone_name": kz.get("name") or None,
        "current_price": actual,
        "entry": entry,
        "entry_kind": entry_kind,
        "entry_zone_reject": (zona_res or {}).get("reason") if zona is None else "",
        "zone_span": (zona_res or {}).get("span"),
        "zone_span_limit": span_limit,
        "sl": sl,
        "tp": tp,
        "sl_distance": sl_dist,
        "sl_distance_source": sl_origen,
        "unit_label": str(_spec_attr(spec, "unit_label", "") or ""),
        "invalidate_level": invalidation,
        "approved": approved,
        "reasons": reasons,
    }


__all__ = [
    "DEFAULT_TP_RATIO",
    "ENTRY_ZONE_MAX_SPAN",
    "ENTRY_ZONE_MAX_SPAN_SL_MULT",
    "SPAN_EPS",
    "ZONE_NO_AXIS",
    "ZONE_TOO_FAR",
    "entry_zone",
    "evaluate_gate",
    "killzone_forced_now",
    "max_zone_span",
    "plan_levels",
]
