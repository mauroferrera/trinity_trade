"""Traduce los nombres de `config/asset_sources_map.yaml` a ingestores reales.

Existe por una asimetría concreta: el YAML nombra **conceptos**
(`cot_report`, `dxy_correlation`, `ecb_fed_calendar`) y el código se llama
**servicios** (`cot_service`, `dxy_service`, `calendar_news`). Sin una tabla que
junte ambos, el YAML no ejecuta nada y no hay forma de notarlo: la fuente simplemente
no aparece en el score y nadie sabe si es que no hay dato o es que nadie la conectó.

Por eso `resolve_all()` no filtra lo que no conoce. Un nombre sin implementación
vuelve como lectura neutra **con el motivo escrito**, y `faltantes()` permite
preguntarlo por adelantado en el arranque. Un `soft_data_sources` que resuelve a
cero fuentes debería delatarlo en el log, no producir un score más bajo sin
explicación.

Los módulos se importan dentro de `resolve()`, no al importar este archivo. Es la
misma regla del resto del paquete: importar no abre red ni crea cachés.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from macro_ingestor.base_ingestor import neutro

#: Nombre en el YAML -> módulo que lo implementa.
IMPLEMENTADAS: Dict[str, str] = {
    "cot_report": "macro_ingestor.forex.cot_service",
    "dxy_correlation": "macro_ingestor.forex.dxy_service",
    "ecb_fed_calendar": "macro_ingestor.forex.calendar_news",
}

#: Declaradas en `asset_sources_map.yaml` y sin implementación. No hay contrato ni
#: endpoint de referencia, así que un esqueleto inventaría un esquema que nadie
#: puede verificar. Se dejan para una fase con fuentes confirmadas.
PENDIENTES: Tuple[str, ...] = (
    "bcb_focus",
    "foreigner_flow",
    "di_futures_yield",
    "news_blackout",
    "fear_and_greed_index",
    "crypto_onchain_whales",
    "funding_rate_monitor",
    "liquidation_heatmap",
)


def resolve(nombre: str) -> Optional[Callable[..., Dict[str, Any]]]:
    """Devuelve el `poll()` de la fuente, o `None` si no está implementada.

    `None` es una respuesta legítima y no un error: el YAML declara fuentes que
    todavía no existen. Lo que no es legítimo es que el `None` se pierda.
    """
    ruta = IMPLEMENTADAS.get(str(nombre or "").strip())
    if ruta is None:
        return None
    modulo = importlib.import_module(ruta)
    poll = getattr(modulo, "poll", None)
    return poll if callable(poll) else None


def resolve_all(nombres: Sequence[str], **kwargs: Any) -> Dict[str, Dict[str, Any]]:
    """Ejecuta `poll()` de cada fuente conocida y devuelve lectura por nombre.

Nunca lanza y nunca se salta un nombre en silencio:

    - nombre sin implementación -> neutro con el motivo;
    - entrada del registro que no importa -> neutro diciendo "registro roto", que es
      un bug nuestro y no una fuente caída, y por eso se nombra distinto;
    - `poll()` que revienta -> neutro con la excepción, porque una fuente macro
      caída no puede tirar el cálculo entero.
    """
    lecturas: Dict[str, Dict[str, Any]] = {}
    for nombre in nombres:
        try:
            poll = resolve(nombre)
        except Exception as exc:  # noqa: BLE001 - el typo del YAML no debe parar el bot
            lecturas[nombre] = neutro(
                nombre, "registro roto: {0}: {1}".format(type(exc).__name__, exc)
            )
            continue
        if poll is None:
            if nombre in PENDIENTES:
                motivo = "sin implementación: pendiente declarado en registry.PENDIENTES"
            else:
                motivo = "sin implementación y no figura en registry.PENDIENTES: fuente olvidada"
            lecturas[nombre] = neutro(nombre, motivo)
            continue
        try:
            lecturas[nombre] = poll(**kwargs)
        except Exception as exc:  # noqa: BLE001 - degradar, nunca propagar
            lecturas[nombre] = neutro(
                nombre, "{0}: {1}".format(type(exc).__name__, exc)
            )
    return lecturas


def faltantes(nombres: Sequence[str]) -> List[str]:
    """Nombres declarados que no resuelven a un ingestor. Para loggear al arrancar.

    A diferencia de `resolve_all()`, aquí una entrada rota **sí** lanza: es una
    comprobación de arranque, y un bug de registro que degrada en silencio en vez
    de delatar es exactamente lo que se quiere evitar aqui.
    """
    return sorted({n for n in nombres if resolve(n) is None})


def implementadas() -> List[str]:
    return sorted(IMPLEMENTADAS)


__all__ = [
    "IMPLEMENTADAS",
    "PENDIENTES",
    "faltantes",
    "implementadas",
    "resolve",
    "resolve_all",
]