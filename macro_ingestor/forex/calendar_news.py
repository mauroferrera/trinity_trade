"""Calendario económico y gate de noticias.

Portado de `REF/ff_calendar.py`. Lo que cambia no es el parseo sino la **política
del gate**, y es la decisión más importante de esta fase.

El problema de la fuente
------------------------
Forex Factory no tiene API. El scraping directo está detrás de Cloudflare (403), así
que REF pasaba por el proxy público `r.jina.ai`, que devuelve la página como
markdown plano. Es decir: **esta fuente es un scraper de una página que puede cambiar
de formato un martes sin avisar**. No hay forma de arreglar eso; solo se puede hacer
que el fallo sea un dato y no una caída.

Las cuatro expresiones regulares de REF son la parte frágil, y por eso
`parse_calendar` es una función pura y separada: se testea con markdown grabado, y
cuando FF cambie el formato el fallo aparece en el test y no en producción a las
8:30 con el mercado cerrado.

La decisión: el gate hace FAIL-OPEN
-----------------------------------
Si la fuente no responde, `news_gate` devuelve `ok=True`. El bot sigue operando.

El razonamiento, que es lo importante:
- El gate existe para no entrar 15 minutos alrededor de un NFP. Es una
  **precaución**, no una condición de supervivencia.
- Un gate que se cierra por un fallo de red protege de un riesgo hipotético
  (entrar sin querer en un NFP) y arriesga uno real (dejar de operar para siempre,
  porque el proxy se cayó un día y nunca vuelve).
- El sesgo del sistema NO es symmetrical: quedarse fuera de un trade malo cuesta una
  oportunidad; quedarse fuera de todos los trades cuesta la cuenta entera.

El riesgo de fail-open se acepta a conciencia y se mitiga con dos cosas: el `reason`
queda escrito en la lectura (para que el post-mortem vea que el gate estaba
degradado), y el impacto mínimo por defecto es `red` (alto), así que solo se bloquea
ante NFP, FOMC y titulares de impacto alto.

La conversión de zona horaria, y su trampa
-----------------------------------------
FF publica las horas en la zona del reloj que se ve en su página, que cambia según
la página que se esté mirando. REF lo resolvía RESTANDO la hora de su propio reloj:
`evento_utc = hora_FF + (ahora_utc - reloj_FF)`. Es un truco que funciona solo si el
reloj se leyó bien, y si no se pudo leer asumía que las horas ya venían en UTC.

Eso es una suposición silenciosa y por eso aquí la lectura lleva `clock_known`: si el
reloj no se leyó, `asof` y los minutos de los eventos NO se convierten y el gate
avisa con `timezone_assumed=True`. Un gate que cree estar en la ventana correcta
cuando en realidad compara horas de dos zonas es peor que un gate que se apaga, y
por eso el impacto de esa incertidumbre es explícito.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from macro_ingestor.base_ingestor import (
    TTLCache,
    http_text,
    reading,
)

#: El proxy devuelve markdown. `day=today` porque el gate se consulta sobre el día en
#: curso: un calendario de la semana entera multiplicaría el parseo y las filas en
#: blanco.
CALENDAR_URL = "https://r.jina.ai/http://www.forexfactory.com/calendar?day=today"
SOURCE = "Forex Factory (vía Jina)"

#: FF codifica el impacto en el nombre del icono. Es lo más frágil del parseo: si
#: cambian el nombre del PNG, el impacto pasa a "bajo" para todo y el gate deja de
#: bloquear sin avisar. Por eso `IMPACT_NAME_RE` está en un sitio solo.
IMPACT_LEVEL = {"red": 3, "ora": 2, "yel": 1}
IMPACT_LABEL = {"red": "Alto", "ora": "Medio", "yel": "Bajo"}

#: 600 s, no 120 como el DXY. El calendario cambia una vez al día, así que más TTL
#: solo añade datos viejos; y menos expondría a FF a un fetch por trade.
CACHE_TTL = 600.0

FULL_DAY = 24 * 60

TIME_RE = re.compile(r"(\d{1,2}:\d{2}[ap]m)")
#: El nombre del icono dentro de una celda. Va suelto, sin el resto de la fila, para
#: que un icono desconocido degrade el impacto sin perder el evento.
IMPACT_NAME_RE = re.compile(r"ff-impact-(red|ora|yel)\.png")
BRANCH_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")

#: El gate aplica a los pares con las monedas de la fila, no a "forex" entero.
CURRENCIES = ("EUR", "USD")


def to_minutes(hm: str) -> Optional[int]:
    """`'1:30pm'` -> minutos desde medianoche. `None` si no encaja.

    El caso que hay que mirar es el mediodía: `12:00pm` son las 12, no las 0, y
    `12:00am` son las 0, no las 12. Sin las dos ramas, un evento del mediodía se
    convierte en medianoche y el gate bloquea el día entero equivocado.
    """
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", str(hm or "").strip().lower())
    if not m:
        return None
    h, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return h * 60 + mn


def _minutes_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def parse_calendar(md: str) -> Tuple[Optional[int], List[Dict[str, Any]]]:
    """Markdown de FF -> `(minutos del reloj FF, eventos)`. Puro, sin red.

    El reloj del sitio es la referencia de zona horaria de toda la página. Va en el
    retorno y no se adivina dentro: quien llama decide si sabe usarlo (el gate, con
    `now_utc`) o si tiene que asumir UTC (un visor que solo muestra la hora).
    """
    clock_min = None
    mclock = re.search(r"\| \[(\d{1,2}:\d{2}[ap]m)\]\([^)]*?timezone", md or "")
    if mclock:
        clock_min = to_minutes(mclock.group(1))

    events: List[Dict[str, Any]] = []
    prev_time: Optional[int] = None
    for raw in (md or "").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue

        tm = TIME_RE.search(line)
        minutes: Optional[int] = None
        time_str = ""
        if tm:
            minutes = to_minutes(tm.group(1))
            time_str = tm.group(1)
            prev_time = minutes
        elif prev_time is not None:
            # Fila que hereda la hora de la anterior: FF agrupa los eventos de una
            # misma hora en filas separadas. Perder la hora haría que un evento de
            # alto impacto saliera con `minutes=None` y el gate lo ignoraría.
            minutes = prev_time

        cells = [c.strip() for c in line.strip("|").split("|")]

        currency = ""
        for c in cells:
            if re.fullmatch(r"[A-Z]{3}", c):
                currency = c
                break

        # El impacto y el título se leen POR SEPARADO a propósito.
        #
        # REF los sacaba de la misma expresión regular, así que si Forex Factory
        # renombraba un icono (`ff-impact-red.png` -> `ff-impact-rojo.png`) la fila
        # entera desaparecía: no se degradaba el impacto, se perdía el evento. Y un
        # NFP que desaparece es un gate que se abre sin decir nada, que es el peor
        # fallo posible en un filtro de noticias.
        #
        # Separados, un icono desconocido degrada el impacto a `yel` y el evento
        # sigue ahí, visible en `events_considered` y en el post-mortem.
        impact = "yel"
        for c in cells:
            im = IMPACT_NAME_RE.search(c)
            if im:
                impact = im.group(1)
                break

        title = ""
        for c in reversed(cells):
            if re.fullmatch(r"[A-Z]{3}", c) or TIME_RE.fullmatch(c.strip()):
                continue
            limpio = BRANCH_RE.sub("", c).strip().strip("*").strip()
            if limpio:
                title = limpio
                break

        if not currency or not title:
            continue

        events.append(
            {
                "time": time_str,
                "minutes": minutes,
                "currency": currency,
                "impact": impact,
                "title": title,
            }
        )
    return clock_min, events


# ---------------------------------------------------------------------------
# El gate (puro)
# ---------------------------------------------------------------------------


def event_utc_minutes(
    event: Dict[str, Any],
    clock_min: Optional[int],
    now_utc: datetime,
) -> Optional[int]:
    """Minutos del reloj FF -> minutos UTC de hoy.

    Si el reloj no se leyó (`clock_min is None`), devuelve los minutos tal cual y la
    lectura lo marca con `timezone_assumed=True`. Asumir UTC en silencio era lo que
    hacía REF: el gate parecía funcionar y comparaba horas de dos zonas.
    """
    if event.get("minutes") is None:
        return None
    if clock_min is None:
        return int(event["minutes"])
    offset = _minutes_of_day(now_utc) - clock_min
    return (int(event["minutes"]) + offset) % FULL_DAY


def high_impact_in_window(
    events: List[Dict[str, Any]],
    clock_min: Optional[int] = None,
    now_utc: Optional[datetime] = None,
    buffer_min: int = 15,
    currencies: Tuple[str, ...] = CURRENCIES,
    min_impact: str = "red",
) -> Dict[str, Any]:
    """¿Hay evento por encima del impacto mínimo dentro de la ventana? Pura.

    El buffer es simétrico a propósito: un dato que sale 10 minutos ANTES de la hora
    marcada ya mueve el mercado, así que esperar solo hacia adelante deja entrar la
    mitad del riesgo.

    El cálculo de `delta` envuelve el día: un evento a las 23:50 con `now` a las
    00:05 da `+1435` sin el módulo, que es "en casi 24 horas" y no "hace 15 minutos".
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    ref = _minutes_of_day(now_utc)
    level = IMPACT_LEVEL.get(min_impact, IMPACT_LEVEL["red"])

    candidates: List[Tuple[Dict[str, Any], int]] = []
    for e in events:
        if e["currency"] not in currencies:
            continue
        if IMPACT_LEVEL.get(e["impact"], IMPACT_LEVEL["yel"]) < level:
            continue
        ev = event_utc_minutes(e, clock_min, now_utc)
        if ev is None:
            continue
        delta = (ev - ref) % FULL_DAY
        if delta > FULL_DAY / 2:
            delta -= FULL_DAY
        if abs(delta) <= buffer_min:
            candidates.append((e, delta))

    if not candidates:
        return {"blocked": False, "event": None, "detail": "sin eventos en ventana"}

    # Mayor impacto primero; a igual impacto, el más cercano.
    candidates.sort(key=lambda it: (-IMPACT_LEVEL[it[0]["impact"]], abs(it[1])))
    ev, delta = candidates[0]
    if delta == 0:
        rel = "ahora"
    elif delta < 0:
        rel = f"hace {-delta} min"
    else:
        rel = f"en {delta} min"
    return {
        "blocked": True,
        "event": ev,
        "detail": "{0} ({1} impacto) {2}: {3}".format(
            ev["title"], IMPACT_LABEL.get(ev["impact"], ev["impact"]), rel, ev["currency"]
        ),
    }


# ---------------------------------------------------------------------------
# El contrato del ingestor
# ---------------------------------------------------------------------------

_cache: Optional[TTLCache] = None


def _get_cache(opener: Any = None, ttl: float = CACHE_TTL, timeout: float = 20.0) -> TTLCache:
    global _cache
    if _cache is None:
        def _fetch() -> Dict[str, Any]:
            body = http_text(
                CALENDAR_URL,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (compatible; trinity-trade/0.1)"},
                opener=opener,
            )
            clock_min, events = parse_calendar(body)
            return {"clock_min": clock_min, "events": events, "raw_len": len(body)}

        _cache = TTLCache(fetch=_fetch, ttl=ttl)
    return _cache


def poll(
    now_utc: Optional[datetime] = None,
    buffer_min: int = 15,
    currencies: Tuple[str, ...] = CURRENCIES,
    min_impact: str = "red",
    opener: Any = None,
    ttl: float = CACHE_TTL,
) -> Dict[str, Any]:
    """Un `MacroReading` con el veredicto del gate. Nunca lanza.

    `ok=True` con `fail_open=True` significa "no hay dato de noticias, y se ha
    decidido operar igual". Es un valor de verdad y por eso viaja en la lectura, no
    solo implícito en la ausencia de datos.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    result = _get_cache(opener=opener, ttl=ttl).get()

    if result.value is None:
        return reading(
            source=SOURCE,
            payload={
                "ok": True,
                "fail_open": True,
                "block": None,
                "timezone_assumed": True,
                "now_utc": int(now_utc.timestamp()),
                "buffer_min": buffer_min,
                "min_impact": min_impact,
            },
            asof_ts=int(now_utc.timestamp()),
            stale=False,
            reason="calendario no disponible: {0}. Fail-open: no se bloquea por red caída.".format(
                result.reason() or "sin caché previa"
            ),
        )

    payload_data = result.value
    events = payload_data.get("events") or []
    clock_min = payload_data.get("clock_min")

    gate = high_impact_in_window(
        events,
        clock_min=clock_min,
        now_utc=now_utc,
        buffer_min=buffer_min,
        currencies=currencies,
        min_impact=min_impact,
    )
    blocked = bool(gate["blocked"])

    last_close = _last_close(events, now_utc, clock_min)
    next_event = _next_event(events, now_utc, clock_min)

    return reading(
        source=SOURCE,
        payload={
            "ok": not blocked,
            "fail_open": bool(result.stale),
            "block": gate["event"] if blocked else None,
            "detail": gate["detail"],
            "timezone_assumed": clock_min is None,
            "events_considered": len(events),
            "relevant": last_close,
            "next": next_event,
            "now_utc": int(now_utc.timestamp()),
            "buffer_min": buffer_min,
            "min_impact": min_impact,
        },
        asof_ts=int(now_utc.timestamp()),
        stale=result.stale,
        reason=(
            "calendario servido de caché vencida: " + result.reason() if result.stale else ""
        ),
    )


def _last_close(
    events: List[Dict[str, Any]],
    now_utc: datetime,
    clock_min: Optional[int],
) -> Optional[Dict[str, Any]]:
    """El evento relevante MÁS INTENSO ya pasado (no el más reciente).

    Es el más intenso a propósito. Para una decisión que se acaba de tomar importa
    "el titular que más movió el mercado y que todavía pesa", no "el titular de hace
    cuatro minutos que no movió nada".
    """
    ref = _minutes_of_day(now_utc)
    past = []
    for e in events:
        if e["currency"] not in CURRENCIES:
            continue
        ev = event_utc_minutes(e, clock_min, now_utc)
        if ev is None:
            continue
        delta = (ev - ref) % FULL_DAY
        if delta > FULL_DAY / 2:
            delta -= FULL_DAY
        if delta <= 0:
            past.append((e, delta))
    if not past:
        return None
    return max(past, key=lambda it: (IMPACT_LEVEL[it[0]["impact"]], it[1]))[0]


def _next_event(
    events: List[Dict[str, Any]],
    now_utc: datetime,
    clock_min: Optional[int],
) -> Optional[Dict[str, Any]]:
    """El evento relevante de mayor impacto por venir."""
    ref = _minutes_of_day(now_utc)
    upcoming = []
    for e in events:
        if e["currency"] not in CURRENCIES:
            continue
        ev = event_utc_minutes(e, clock_min, now_utc)
        if ev is None:
            continue
        delta = (ev - ref) % FULL_DAY
        if delta > FULL_DAY / 2:
            delta -= FULL_DAY
        if delta > 0:
            upcoming.append((e, delta))
    if not upcoming:
        return None
    return max(upcoming, key=lambda it: (IMPACT_LEVEL[it[0]["impact"]], -it[1]))[0]


def clear_cache() -> None:
    """Olvida el calendario cacheado y su `opener`.

    Se descarta la **instancia**, no solo el valor. Invalidar el valor dejando la
    instancia standing dejaría vivo el `fetch` capturado del primer `opener` que
    se pasó, y el siguiente test que inyecte el suyo seguiría hablando con la
    fuente del anterior. Es un fallo silencioso que produce tests que pasan probando
    la cosa equivocada.
    """
    global _cache
    if _cache is not None:
        _cache.invalidate()
    _cache = None


__all__ = [
    "CACHE_TTL",
    "CURRENCIES",
    "IMPACT_LEVEL",
    "IMPACT_LABEL",
    "SOURCE",
    "clear_cache",
    "event_utc_minutes",
    "high_impact_in_window",
    "parse_calendar",
    "poll",
    "to_minutes",
]