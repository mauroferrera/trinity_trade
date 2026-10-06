"""`NewsPort` sobre `macro_ingestor`: el calendario y las fuentes suaves del YAML.

Por qué un servicio y no el endpoint
------------------------------------
REF tenía el gate de noticias repartido en tres sitios: `_watcher_news_gate()` en
`app.py`, `ff.news_gate()` dentro del módulo del calendario, y el endpoint
`/api/analysis/...` que lo llamaba con los parámetros de la config **leídos con
claves planas** que no existen en el YAML anidado. El resultado era un gate que
funcionaba con los defaults sin que nadie lo notara, en la ruta que decide si se
opera.

Aquí el gate se resuelve en un sitio, con la config anidada (`execution.news_buffer_min`,
`execution.news_gate_impact` si existe, `data_sources.news`), y se publica tanto en
la lectura del agente como en el endpoint. Si el calendario no se puede leer, la
respuesta es la de la Fase 4: `ok=True` con `fail_open=True` EXPLÍCITO. Un gate
fail-open que no declara que ha fallado por dentro es indistinguible de un gate que
ha visto el calendario y no ha encontrado nada.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from macro_ingestor import registry

#: Fuente del YAML que trae el calendario económico.
CALENDAR_SOURCE = "ecb_fed_calendar"

DEFAULT_BUFFER_MIN = 15
DEFAULT_MIN_IMPACT = "red"


def _cfg_val(cfg: Dict[str, Any], *ruta: str) -> Any:
    """Lee una clave del YAML anidado sin reventar si falta un tramo."""
    actual: Any = cfg
    for paso in ruta:
        if not isinstance(actual, dict):
            return None
        actual = actual.get(paso)
    return actual


class MacroNews:
    """El calendario y las fuentes macro que el YAML declara.

    `store` es opcional y solo se usa para el `buffer_min` y el `min_impact` de la
    config y para las columnas del COT ya descargadas. Sin él, el gate funciona con
    los defaults declarados aquí, que son los mismos que el YAML trae.
    """

    __slots__ = ("_store", "_poll")

    def __init__(self, store: Optional[Any] = None, poll: Optional[Any] = None) -> None:
        self._store = store
        # Inyectable para los tests: la red no se toca nunca en la suite.
        self._poll = poll

    # -- configuración ---------------------------------------------------------

    def config(self) -> Dict[str, Any]:
        if self._store is None:
            return {}
        try:
            return self._store.get_trading_config() or {}
        except Exception:  # noqa: BLE001 - sin config, defaults declarados
            return {}

    def _buffer_min(self) -> int:
        valor = _cfg_val(self.config(), "execution", "news_buffer_min")
        try:
            return int(valor) if valor is not None else DEFAULT_BUFFER_MIN
        except (TypeError, ValueError):
            return DEFAULT_BUFFER_MIN

    def _min_impact(self) -> str:
        valor = _cfg_val(self.config(), "execution", "news_gate_impact")
        return str(valor or DEFAULT_MIN_IMPACT)

    def _enabled(self) -> bool:
        valor = _cfg_val(self.config(), "data_sources", "news")
        return True if valor is None else bool(valor)

    # -- NewsPort ---------------------------------------------------------------

    def news(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """El `MacroReading` del calendario, o un neutro con el motivo.

        `symbols` se acepta por contrato (`NewsPort`) y aquí NO filtra nada: el
        calendario de Forex Factory es por divisas y el servicio ya decide con sus
        `CURRENCIES`. Aceptarlo y no usarlo es mejor que inventar un filtro que
        parece aplicado y no lo está; quien filtró de verdad fue
        `calendar_news.poll`.
        """
        if not self._enabled():
            from macro_ingestor.base_ingestor import reading  # noqa: PLC0415 - evita el import al cargar la API

            # Desactivado a propósito NO es un fallo: se declara con `fail_open`
            # en `False` para que el gate no lo confunda con "no lo sé" (abajo).
            return reading(
                source=CALENDAR_SOURCE,
                payload={
                    "ok": True,
                    "fail_open": False,
                    "block": None,
                    "detail": "noticias desactivadas en data_sources.news",
                    "disabled": True,
                },
                reason="noticias desactivadas en data_sources.news",
            )
        poll = self._poll or registry.resolve(CALENDAR_SOURCE)
        if poll is None:
            from macro_ingestor.base_ingestor import neutro  # noqa: PLC0415 - ídem

            return neutro(
                CALENDAR_SOURCE,
                "el calendario no está implementado en macro_ingestor.registry",
            )
        try:
            return poll(buffer_min=self._buffer_min(), min_impact=self._min_impact())
        except Exception as exc:  # noqa: BLE001 - degradar, nunca propagar (D-025)
            from macro_ingestor.base_ingestor import neutro  # noqa: PLC0415 - ídem

            return neutro(CALENDAR_SOURCE, "{0}: {1}".format(type(exc).__name__, exc))

    # -- el gate de noticias, para el watcher y la puerta de ejecución ----------

    def gate(self) -> Dict[str, Any]:
        """El veredicto del gate, con `fail_open` explícito.

        Es el mismo objeto que devuelve `news()`, desempaquetado: `ok`, `block`,
        `fail_open`. Quien cierra la puerta lee `block`, y quien lee sin bloque
        (`fail_open`) tiene que poder decirlo en voz alta.

        El caso que obliga a esto: una lectura SIN payload (`neutro`) no trae
        veredicto, y el gate por defecto abriría en silencio. Sería indistinguible
        de un calendario limpio, que es justo la confusión que decide si se opera
        con una noticia encima. Por eso `fail_open` sale `True` cuando no hay
        veredicto, y el motivo va en `reason`.
        """
        lectura = self.news() or {}
        payload = lectura.get("payload")
        sin_veredicto = payload is None
        datos = payload or {}
        veredicto = {
            "ok": True if sin_veredicto else bool(datos.get("ok", True)),
            "block": datos.get("block"),
            "detail": datos.get("detail"),
            "fail_open": True if sin_veredicto else bool(datos.get("fail_open", False)),
            "timezone_assumed": bool(datos.get("timezone_assumed", False)),
            "disabled": bool(datos.get("disabled", False)),
            "source": lectura.get("source"),
            "stale": bool(lectura.get("stale", False)),
            "reason": lectura.get("reason") or "",
        }
        if sin_veredicto and not veredicto["reason"]:
            veredicto["reason"] = "el calendario no devolvió veredicto"
        return veredicto

    def blocked(self) -> bool:
        """¿Hay noticia de alto impacto en la ventana? Un booleano y ya."""
        return bool(self.gate().get("block"))

    # -- otras fuentes del YAML --------------------------------------------------

    def lecturas(self, nombres: Sequence[str], **kwargs: Any) -> Dict[str, Dict[str, Any]]:
        """Ejecuta las fuentes que el YAML declara y devuelve lectura por nombre.

        No filtra en silencio: un nombre sin implementación vuelve como neutro con
        el motivo (D-027), y `registry.faltantes()` dice cuáles son.
        """
        return registry.resolve_all(list(nombres), **kwargs)

    def cot(self) -> Optional[Dict[str, Any]]:
        """El sesgo del COT desde lo YA descargado en la base, sin red.

        La descarga es de `macro_ingestor.forex.cot_service`; aquí solo se lee lo
        que hay en las columnas, porque el snapshot se llama cada 10 s y una
        descarga por snapshot sería una llamada a la CFTC cada 10 s.
        """
        if self._store is None:
            return None
        try:
            from macro_ingestor.forex import cot_service  # noqa: PLC0415 - perezoso a propósito

            return cot_service.build_report(self._store.list_cot_reports())
        except Exception:  # noqa: BLE001 - el COT es un extra del score
            return None


__all__ = ["CALENDAR_SOURCE", "DEFAULT_BUFFER_MIN", "DEFAULT_MIN_IMPACT", "MacroNews"]
