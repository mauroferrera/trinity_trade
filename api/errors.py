"""Traducción de excepciones de las capas inferiores a respuestas HTTP.

Por qué este módulo existe
--------------------------
REF tenía dos `@app.exception_handler` (líneas 92-105 de `app.py`) registrados
para `RuntimeError` y para `concurrent.futures.TimeoutError`, y los dos devolvían
JSON 503. El motivo era bueno —el frontend buscaba `error` y con texto plano
mostraba "respuesta inválida", escondiendo el mensaje de MT5— pero el método era
demasiado ancho: `RuntimeError` es la clase base de casi todos los errores que
existen, así que esos handlers también se tragaban un `KeyError` disfrazado de
`RuntimeError` de cualquier endpoint y lo devolvían como "no hay conexión con
MetaTrader 5".

Aquí cada.handler se registra sobre la clase que SIGNIFICA algo:

| excepción                  | código | qué le está diciendo al cliente           |
|----------------------------|--------|------------------------------------------|
| `SymbolNotFound`           | 404    | el bróker no publica ese símbolo          |
| `AdapterError`             | 503    | no se pudo hablar con el proveedor        |
| `IngestorError`            | 502    | la fuente macro no se pudo leer           |
| `AgentPortError`           | 503    | al agente le falta un puerto cableado     |
| `ConfigUnavailable`        | 503    | no hay `strategy` que cargar              |
| `ValueError` de parámetros | 400    | el cliente pidió algo imposible           |

Un bug de código (`TypeError`, `KeyError`, `AttributeError`) NO tiene handler:
sube y sale 500 con el traceback en el log, que es donde tiene que estar.

`ValueError` es el único caso ambiguo y se acepta a propósito: es lo que lanzan
`core.market_view` y `core.lot_calculator` cuando un parámetro está fuera de rango,
y esos son errores de la petición. El riesgo de tragarse un `ValueError` nuestro
es real pero pequeño, y el castigo (un 400 con el motivo en vez de un 500 en el log)
es el que corresponde a un parámetro malo.

El cuerpo siempre lleva `error` (el frontend lo busca por ese nombre, igual que
en REF) y, cuando el fallo tiene una razón conocida, también `reason` para que un
cliente pueda distinguir "no hay terminal" de "no hay datos".
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from adapters.base_adapter import AdapterError, SymbolNotFound
from agent.ports import AgentPortError
from core.strategy import StrategyConfigError
from database.store import ConfigUnavailable
from macro_ingestor.base_ingestor import IngestorError

#: `(excepción, código, mensaje por defecto)`. El mensaje por defecto solo se usa
#: cuando la excepción no trae texto: un `AdapterError` sin mensaje es un fallo sin
#: explicación y "no se pudo leer el proveedor" al menos acota el sitio.
MANEJADORES: Tuple[Tuple[type, int, str], ...] = (
    (SymbolNotFound, 404, "El bróker no publica ese símbolo."),
    (AdapterError, 503, "No se pudo hablar con el proveedor de datos."),
    (IngestorError, 502, "No se pudo leer la fuente macro."),
    (AgentPortError, 503, "Falta un puerto del agente por cablear."),
    (ConfigUnavailable, 503, "No hay configuración de estrategia cargada."),
    (StrategyConfigError, 422, "Configuración de estrategia inválida."),
    (ValueError, 400, "Parámetro fuera de rango."),
)


def _cuerpo(mensaje: str, codigo: int, por_defecto: str) -> Dict[str, Any]:
    texto = str(mensaje).strip() or por_defecto
    return {"error": texto, "status": codigo}


def _handler_para(codigo: int, por_defecto: str) -> Callable:
    """Construye el handler asíncrono de una clase de error."""

    async def handler(_request: Any, exc: BaseException) -> JSONResponse:
        return JSONResponse(_cuerpo(str(exc), codigo, por_defecto), status_code=codigo)

    return handler


def install(app: FastAPI) -> None:
    """Registra los handlers de las capas de datos sobre `app`."""

    for exc, codigo, por_defecto in MANEJADORES:
        app.add_exception_handler(exc, _handler_para(codigo, por_defecto))


__all__ = ["MANEJADORES", "install"]
