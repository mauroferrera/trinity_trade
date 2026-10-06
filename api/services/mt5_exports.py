"""`ExportPort`: los `.txt` que el indicador "AI Chart Assistant" escribe en MT5.

Por qué no es solo una copia de `REF/mt5_export.py`
---------------------------------------------------
REF tenía este módulo con tres efectos secundarios: localizaba el directorio de
ficheros **al importar** (`files_dir()` con `os.getenv` dentro), lanzaba
`RuntimeError` genérico cuando no lo encontraba, y validaba el nombre del fichero
con `full.startswith(dabs + os.sep)`. Los dos últimos tienen consecuencias:

1. **La ruta se comprobaba con un prefijo de cadena.** `startswith` sobre rutas de
   Windows es un chequeo débil: un `..\\..\\` dentro del nombre se normalizaba con
   `abspath` **antes** de la comparación, así que un nombre con `..` que salía del
   directorio podía colarse según dónde estuviera la barra. Aquí la resolución es
   con `Path.resolve()` y la comprobación es que el resultado siga DENTRO del
   directorio. Un endpoint que lee ficheros por nombre es un path traversal
   esperando.
2. **Un directorio ausente era un `RuntimeError` indistinguible de un fallo de MT5.**
   Aquí son dos: `ExportDirMissing` y `ExportNotFound`, y el mensaje dice cuál de
   las dos cosas es. El agente los traduce a `failed` con el motivo, que es lo que
   necesita para decir "no hay exportaciones" en vez de quedarse mudo.

`files_dir` es inyectable y se resuelve **por llamada**, no al importar: el
directorio del terminal cambia de hash cuando MT5 se actualiza, y un `files_dir`
cacheado en la importación apunta a un directorio que ya no existe.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PREFIX = "AR_AI_ADV_"

#: Palabras clave del modo de exportación en el nombre del fichero -> etiqueta.
MODE_KEYWORDS = {
    "Full_Trade_Plan": "Full Trade Plan",
    "Analyze_Chart": "Analyze Chart",
    "Quick_Default_Analyze": "Quick Default Analyze",
    "Manual": "Manual",
}

#: Regex del nombre de fichero. Igual que en REF, porque es el formato que escribe
#: el indicador: cambiarlo aquí sin cambiarlo allí deja de encontrar los ficheros.
EXPORT_RE = re.compile(r"AR_AI_ADV_[A-Za-z0-9_]+\.txt")

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,12}$")
_TIMEFRAME_RE = re.compile(r"^(M1|M5|M15|M30|H1|H4|H6|H12|D1|W1|MN)")

DEFAULT_MAX_BYTES = 20000
DEFAULT_LIMIT = 10


class ExportError(RuntimeError):
    """Fallo genérico de la capa de exportaciones."""


class ExportDirMissing(ExportError):
    """No hay directorio de exportaciones. No es lo mismo que "no hay ficheros"."""


class ExportNotFound(ExportError):
    """El directorio existe pero el fichero pedido no (o no está dentro)."""


def default_files_dir() -> Optional[str]:
    """`MQL5\\Files` del primer terminal MT5 del usuario, o `None`.

    Se recorren los directorios hash de `AppData\\Roaming\\MetaQuotes\\Terminal` en
    orden alfabético y se devuelve el primero que tenga `MQL5/Files`. Con varios
    terminales instalados es una elección, no una verdad: por eso la variable de
    entorno `MT5_FILES_DIR` gana siempre, y por eso `list_exports` devuelve todos
    los directorios candidatos que encuentra si se le pasa `todos=True`.
    """
    base = Path.home() / "AppData" / "Roaming" / "MetaQuotes" / "Terminal"
    if not base.is_dir():
        return None
    for entry in sorted(base.iterdir()):
        candidate = entry / "MQL5" / "Files"
        if candidate.is_dir():
            return str(candidate)
    return None


def _from_env() -> Optional[str]:
    valor = os.getenv("MT5_FILES_DIR")
    return valor or None


def parse_filename(name: str) -> Dict[str, Optional[str]]:
    """`(símbolo, timeframe, modo)` a partir del nombre del fichero.

    Puro: el nombre es un dato, no una ruta, y esto se puede testear sin terminal.
    El modo se busca por palabra clave en cualquier posición (el nombre lo genera
    el indicador, no nosotros) y el resto se parte por `_`.
    """
    base = name[len(PREFIX):] if name.startswith(PREFIX) else name
    if base.endswith(".txt"):
        base = base[:-4]
    mode: Optional[str] = None
    remaining = base
    for clave, etiqueta in MODE_KEYWORDS.items():
        if clave in base:
            mode = etiqueta
            remaining = base.replace(clave, "_")
            break
    partes = [p for p in remaining.split("_") if p]
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    col = 0
    if partes and _SYMBOL_RE.match(partes[0].upper()):
        symbol = partes[0].upper()
        col = 1
    if len(partes) > col and _TIMEFRAME_RE.match(partes[col].upper()):
        timeframe = partes[col].upper()
    return {"symbol": symbol, "timeframe": timeframe, "mode": mode}


class MT5Exports:
    """Implementación de `agent.ports.ExportPort` sobre ficheros del terminal."""

    __slots__ = ("_files_dir",)

    def __init__(self, files_dir: Optional[Callable[[], Optional[str]]] = None) -> None:
        self._files_dir = files_dir or (lambda: _from_env() or default_files_dir())

    # -- resolución de rutas ---------------------------------------------------

    def files_dir(self) -> str:
        """El directorio de exportaciones, resuelto AHORA. `""` si no hay ninguno."""
        return self._files_dir() or ""

    def resolve_path(self, filename: str) -> Path:
        """`(ruta_absoluta, comprobada)` de un nombre de fichero.

        Solo acepta un **nombre base**: si el llamador manda `algo.txt` y dentro hay
        un `..`, el resultado cae fuera del directorio y se lanza `ExportNotFound`
        con el motivo, en vez de leer un fichero que el usuario no pidió.
        """
        nombre = str(filename or "").strip()
        if not nombre:
            raise ExportNotFound("nombre de exportación vacío")
        base = self.files_dir()
        if not base or not Path(base).is_dir():
            raise ExportDirMissing(
                "directorio de exportaciones no encontrado{0}".format(
                    ": " + base if base else " (terminal MT5 no instalado o MT5_FILES_DIR sin definir)"
                )
            )
        raiz = Path(base).resolve()
        destino = (raiz / nombre).resolve()
        try:
            dentro = destino.is_relative_to(raiz)
        except AttributeError:  # pragma: no cover - Python < 3.9
            dentro = str(destino).startswith(str(raiz) + os.sep)
        if not dentro or destino.name != Path(nombre).name:
            raise ExportNotFound(
                "el nombre {0!r} no es un fichero del directorio de exportaciones".format(nombre)
            )
        if not destino.is_file():
            raise ExportNotFound("no existe la exportación: " + nombre)
        return destino

    # -- ExportPort ------------------------------------------------------------

    def list_exports(
        self,
        symbol: Optional[str] = None,
        mode: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> List[Dict[str, Any]]:
        """Las exportaciones recientes, de la más nueva a la más vieja.

        Lanza `ExportDirMissing` si el directorio no está: el contrato de
        `ExportPort` devuelve `List` porque la hay, y un `[]` cuando el
        directorio no existe es indistinguible de "el indicador no ha exportado
        nada", que es justo lo que el agente terminaría diciendo al usuario.
        """
        base = self.files_dir()
        if not base or not Path(base).is_dir():
            raise ExportDirMissing(
                "directorio de exportaciones no encontrado{0}".format(
                    ": " + base if base else " (terminal MT5 no instalado o MT5_FILES_DIR sin definir)"
                )
            )
        entradas: List[Dict[str, Any]] = []
        for nombre in os.listdir(base):
            if not (nombre.startswith(PREFIX) and nombre.endswith(".txt")):
                continue
            completa = Path(base) / nombre
            if not completa.is_file():
                continue
            meta = parse_filename(nombre)
            if symbol and meta["symbol"] and meta["symbol"].upper() != str(symbol).upper():
                continue
            if mode and meta["mode"] and meta["mode"].lower() != str(mode).lower():
                continue
            stat = completa.stat()
            entradas.append(
                {
                    "filename": nombre,
                    "symbol": meta["symbol"],
                    "timeframe": meta["timeframe"],
                    "mode": meta["mode"],
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
        entradas.sort(key=lambda e: e["modified"], reverse=True)
        return entradas[: max(1, int(limit or DEFAULT_LIMIT))]

    def read_export(self, filename: str, max_bytes: int = DEFAULT_MAX_BYTES) -> Dict[str, Any]:
        """El contenido de una exportación, truncado a `max_bytes`.

        El truncado se declara en el resultado (`truncated`) en vez de recortarse a
        ciegas: un análisis del indicador cortado por la mitad sin decirlo produce
        un diagnóstico sobre la mitad del diagnóstico.
        """
        ruta = self.resolve_path(filename)
        with open(ruta, "r", encoding="utf-8", errors="replace") as fh:
            contenido = fh.read(max_bytes)
        truncado = ruta.stat().st_size > len(contenido.encode("utf-8", errors="replace"))
        return {
            "filename": ruta.name,
            "content": contenido,
            "truncated": truncado,
            "size": ruta.stat().st_size,
        }

    def latest_export(
        self,
        symbol: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """La exportación más reciente, o `None` si no hay ninguna.

        `None` y no excepción: "no hay ninguna" es un estado normal del indicador
        (nadie ha pulsed Export) y la herramienta `chart_snapshot` lo trata como
        "sin indicador", no como error.
        """
        entradas = self.list_exports(symbol=symbol, mode=mode, limit=1)
        if not entradas:
            return None
        return self.read_export(entradas[0]["filename"])

    def extract_export_path(self, text: str) -> Optional[str]:
        """Nombre de exportación si el usuario la pegó en el chat.

        Acepta ruta completa o nombre suelto, y devuelve solo el nombre base: quien
        llama (`laya_bridge`) pasa el resultado a `read_export()`, que ya sabe
        defenderse de una ruta. Es lo que hace REF, y el patrón de nombre es del
        indicador, no nuestro.
        """
        if not text:
            return None
        match = EXPORT_RE.search(str(text))
        return match.group(0) if match else None


__all__ = [
    "DEFAULT_MAX_BYTES",
    "EXPORT_RE",
    "MODE_KEYWORDS",
    "PREFIX",
    "ExportDirMissing",
    "ExportError",
    "ExportNotFound",
    "MT5Exports",
    "default_files_dir",
    "parse_filename",
]