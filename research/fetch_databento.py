"""Descarga cinta real de Databento (6E, CME GLBX.MDP3) para calibrar el order flow.

Es el eslabón entre la API de pago y `core/orderflow_config.py`: todo lo que el
motor asume sobre la distribución de tamaños (mediana ~2, media ~4.4, gate de
size >= 75) sale de aquí, no de un mock. La regla de este módulo es que NO
calibra nada: baja, deja Parquet con su sidecar de procedencia y, si se pide,
un .jsonl en el shape de `adapters.synthetic_feed.load_fixture`. Quien decide
umbrales es `research/calibrate_orderflow.py`.

Por qué existe la selección automática de contrato
-------------------------------------------------
`6E.c.0` es "próximo a expirar" por FECHA, y eso no es el contrato que negocia
el mercado. Medido el 2026-10-05:

    6E.c.0 -> 6EV6 (octubre)      44 trades en 4 h de NY
    6EZ6    (diciembre)        14.660 trades en la misma ventana

CME lista los 12 meses pero el volumen vive en el trimestral; tras el roll, el
continuous apunta a un contrato moribundo con ~100 prints por sesión. Bajar eso
y calibrar contra ello produce un fixture inútil SIN NINGÚN ERROR: el download
funciona, los números existen, solo que son los de un mercado vacío. Por eso el
default --symbol 6E mide los meses vivos y se queda con el más líquido.

Uso::

    # Los 3 días naturales 2026-10-05..07 (--end es EXCLUSIVO)
    python research/fetch_databento.py --start 2026-10-05 --end 2026-10-08

    # Solo las ventanas de NY (13:30-20:00 UTC), contra un contrato concreto
    python research/fetch_databento.py --start 2026-10-05T13:30:00 \
        --end 2026-10-08T20:00:00 --symbol 6EZ6

    # Y además emitir la fixture de tests
    python research/fetch_databento.py --start 2026-10-05 --end 2026-10-08 \
        --jsonl tests/fixtures/orderflow_6e_real.jsonl

100% offline salvo la llamada a Databento: la selección de contrato y la
escritura del .jsonl son funciones puras y se testean sin red.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# `python research/fetch_databento.py` pone `research/` en sys.path[0], no la
# raíz: sin esto, `import core.paths` falla siempre y el error (ModuleNotFound)
# no dice nada sobre el problema real, que sería una ruta mal calculada.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.paths import (  # noqa: E402
    DATABENTO_RAW_DIR,
    PROJECT_DIR,
    data_root_guard_error,
    ensure_dir,
)

# Meses de contrato de futuros. No es un alfabeto: no existe "6EO6" ni "6EY6",
# y un generador que asumiera los 12 meses del calendario pediría símbolos que
# Databento devuelve como inexistentes.
MONTH_CODES = "FGHJKMNQUVXZ"

SCHEMA = "trades"

__all__ = [
    "MONTH_CODES",
    "SCHEMA",
    "classify_symbol",
    "load_env",
    "parse_instant",
    "symbology_window",
    "jsonl_record",
    "select_symbol",
    "main",
]


# --- Entorno -----------------------------------------------------------------


def fix_console_encoding() -> None:
    """Alinea stdout/stderr con la codepage REAL de la consola Windows.

    Python infiere la codificación de salida de la configuración regional
    (cp1252 en esta máquina) mientras que la consola ejecuta otra (cp850, la
    página DOS Latina-1). Los bytes de "más" se escriben en cp1252 y la consola
    los lee como cp850, y el acento sale como un símbolo raro en justo los
    mensajes que hay que poder interpretar.

    Solo se toca si la salida va a una consola: con la salida redirigida a
    fichero o capturada por pytest (`isatty()` falso) hay que dejar el default,
    que es el que espera quien abre el fichero.
    """
    if os.name != "nt":
        return
    try:
        if not (sys.stdout.isatty() and sys.stderr.isatty()):
            return
        import ctypes

        codepage = ctypes.windll.kernel32.GetConsoleOutputCP() or 0
        if not codepage:
            return
        encoding = f"cp{codepage}"
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding=encoding, errors="replace")
    except Exception:  # noqa: BLE001 - esto es cosmético y no puede tumbar la bajada
        pass


def load_env() -> Path | None:
    """Carga `PROJECT_DIR/.env` en el entorno del proceso y devuelve su ruta.

    Se llama acá y no en `core/`: `test_core_purity.py` prohíbe `dotenv` dentro
    de `core/` a propósito, porque un módulo que lee el disco al importarse deja
    de ser determinista. La clave tampoco se pasa como argumento de línea de
    comandos: queda en el historial de la shell.
    """
    env_file = Path(PROJECT_DIR) / ".env"
    if not env_file.is_file():
        return None
    try:
        from dotenv import load_dotenv
    except ImportError:
        # python-dotenv es opcional: si no está, DATABENTO_API_KEY puede venir
        # del entorno del proceso (CI, systemd) y eso también es válido.
        return None
    # override=False: lo que el proceso ya trae manda. Permite sobrescribir la
    # clave sin editar el fichero.
    load_dotenv(env_file, override=False)
    return env_file


# --- Tiempo ------------------------------------------------------------------


def parse_instant(value: str, *, end: bool = False) -> str:
    """Normaliza `--start`/`--end` a ISO 8601 sin zona (que Databento toma como UTC).

    Una fecha suelta significa medianoche de ese día, y `--end` es EXCLUSIVO
    como en la propia API: `--start 2026-10-05 --end 2026-10-08` cubre los días
    5, 6 y 7 enteros. Con hora explícita se respeta tal cual, que es como se pide
    una ventana de sesión (13:30-20:00 UTC). `end=True` existe solo para que la
    firma documente qué extremo se está parseando; no altera el valor, y es
    deliberado: añadir un día aquí hizo que un `--end` correcto cayera en el
    futuro y la API respondiera con un error de licencia que no menciona fechas.

    Devuelve string y no `datetime` porque la API de Databento acepta ISO y esto
    deja el valor impreso tal cual se usó en el sidecar.
    """
    text = value.strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"instante no reconocido: {value!r} (use 2026-10-05 o 2026-10-05T13:30:00)"
        ) from exc
    if "T" in text or " " in text:
        return text.replace(" ", "T").replace("Z", "")
    day = datetime.strptime(text, "%Y-%m-%d").date()
    return f"{day.isoformat()}T00:00:00"


def symbology_window(start_iso: str, end_iso: str) -> Tuple[str, str]:
    """Deriva el rango de fechas INCLUSIVO que `symbology.resolve` necesita.

    Sobre el rango `[start, end)` del timeseries hay que hacer dos cosas:

    - La simbología es INCLUSIVA y no acepta `start_date == end_date`
      (`data_date_range_start_on_or_after_end`), así que el fin se empuja UN DÍA
      más allá del último cubierto. Es un superconjunto deliberado: solo se usa
      para preguntar qué contrato existía, y ensancharlo un día no cambia la
      respuesta pero sí evita el caso borde de una ventana de un solo día.
    - El fin nunca es futuro. Con fechas futuras Databento contesta
      `dataset_unavailable_range` traducido como "requires a subscription
      and/or license", que es el error más engañoso de toda la API: no menciona
      fechas, así que parece un problema de cuenta cuando es un problema de
      calendario.

    Si aun así el inicio queda por delante del fin (una ventana que empieza
    HOY), se mira un día HACIA ATRÁS en vez de fallar: el contrato de un mes no
    cambia de identidad de un día para otro.
    """
    today = datetime.now(timezone.utc).date()
    start_day = datetime.fromisoformat(start_iso).date()
    covered_last = datetime.fromisoformat(end_iso) - timedelta(microseconds=1)
    end_day = min(covered_last.date() + timedelta(days=1), today)
    if end_day <= start_day:
        start_day = end_day - timedelta(days=1)
    if end_day <= start_day:
        raise SystemExit(
            f"la ventana {start_iso}..{end_iso} no deja ningún día con datos "
            f"(hoy es {today.isoformat()})"
        )
    return start_day.isoformat(), end_day.isoformat()


# --- Conversión de fila ------------------------------------------------------


def _epoch_seconds(ts: Any) -> Optional[float]:
    """Instante a epoch SECONDS con coma flotante, tolerando pandas.

    `DBNStore.to_df()` entrega `ts_event` como `datetime64[ns, UTC]`, es decir
    un `pandas.Timestamp`: `float()` sobre él lanza TypeError, que es el error
    que aparece si se asume que el valor ya es numérico. `Timestamp` hereda de
    `datetime` y expone `.value` en nanosegundos desde el epoch.

    Los números se tratan como nanosegundos, que es lo que publica Databento, a
    menos que no puedan serlo: un epoch en ns para 2026 ronda 1.8e18 y en
    segundos 1.8e9, así que 1e14 separa los dos sin ambigüedad. Eso deja que
    esta misma función sirva para los fixtures existentes, que ya guardan
    segundos (1.7e9).
    """
    if isinstance(ts, datetime):
        if hasattr(ts, "value"):
            return float(ts.value) / 1e9
        aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        return aware.timestamp()
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        value = float(ts)
        if value == 0:
            return 0.0
        return value / 1e9 if abs(value) > 1e14 else value
    return None


def jsonl_record(row: Any) -> Optional[Dict[str, Any]]:
    """Convierte una fila de Databento al shape de `load_fixture`, o None si no sirve.

    Descarta `side` "N" (sin agresor identificado): `load_fixture` rechaza el
    shape y `OrderFlowEngine.add_trade` devuelve None, así que meterlo en el
    fixture solo añadiría líneas que ningún test podría consumir.

    El núcleo trabaja en epoch SECONDS con coma flotante (es lo que imprimen los
    fixtures existentes y lo que espera `core.orderflow_engine` para su formateo
    de alertas).
    """
    side = row.get("side")
    if side not in ("A", "B"):
        return None
    ts = row.get("ts_event")
    if ts is None:
        ts = row.get("ts_recv")
    if ts is None:
        return None
    seconds = _epoch_seconds(ts)
    price = row.get("price")
    size = row.get("size")
    if seconds is None or price is None or size is None:
        return None
    return {
        "ts": seconds,
        "price": float(price),
        "size": int(size),
        "side": str(side),
    }


def write_jsonl(rows: Iterable[Dict[str, Any]], path: Path, limit: int = 0) -> int:
    """Escribe los trades válidos como JSONL y devuelve cuántos escribió."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            rec = jsonl_record(row)
            if rec is None:
                continue
            fh.write(json.dumps(rec, separators=(", ", ": ")) + "\n")
            written += 1
            if limit and written >= limit:
                break
    return written


# --- Selección de contrato ---------------------------------------------------


def classify_symbol(symbol: str) -> str:
    """Clasifica `--symbol` en "continuous" | "contract" | "root". Puro, sin red.

    Un contrato es `<raíz><mes><año>` (6EZ6, ESZ26): se detecta exigiendo que
    se hayan separado DÍGITOS del final y que lo que queda termine en un código
    de mes. Sin esa doble condición, una raíz que acabe en letra de mes ("RTX",
    X = noviembre) se clasificaría como contrato y se pediría a Databento un
    símbolo inexistente.
    """
    if ".c." in symbol.lower():
        return "continuous"
    stripped = symbol.rstrip("0123456789")
    if len(stripped) < len(symbol) and stripped and stripped[-1] in MONTH_CODES:
        return "contract"
    return "root"


def select_symbol(counts: Dict[str, int]) -> str:
    """Elige el símbolo vivo con más trades en la ventana. Puro, sin red.

    Desempata por nombre para que la salida sea reproducible: dos meses con el
    mismo recuento no deben alternar entre corridas y hacer que el fixture
    cambie de contrato sin que nadie lo note.

    Lanza ValueError con la lista de lo que se midió: un "no hay datos" tiene
    que decir QUÉ se miró, no devolver None para que el error aparezca más
    abajo como una KeyErorr sobre un dict vacío.
    """
    viable = {s: n for s, n in counts.items() if n and n > 0}
    if not viable:
        medidos = ", ".join(f"{s}={n}" for s, n in sorted(counts.items())) or "nada"
        raise ValueError(
            "ningún símbolo tiene trades en la ventana (medidos: "
            f"{medidos}). ¿Rango de fechas mal, o el mercado estaba cerrado?"
        )
    return max(sorted(viable), key=lambda s: viable[s])


# --- Cliente -----------------------------------------------------------------


def _client() -> Any:
    """Cliente histórico de Databento.

    En 0.85 es `db.Historical()` (antes `db.History`) y lee
    `DATABENTO_API_KEY` del entorno; no se le pasa la clave como argumento para
    que no aparezca en un `ps` ni en un traceback.
    """
    import databento as db

    if not os.environ.get("DATABENTO_API_KEY"):
        raise SystemExit(
            "falta DATABENTO_API_KEY: cree un .env a partir de .env.example "
            "(la clave vive en el .env de la referencia y NUNCA se commitea)."
        )
    return db.Historical()


def resolve_symbols(
    client: Any,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    stype_in: str = "raw_symbol",
) -> Dict[str, int]:
    """Resuelve símbolos a instrument_id. Devuelve solo los que existen.

    Los contratos ya vencidos no resuelven, y esa ausencia es información, no
    un error: es lo que permite filtrar los meses muertos antes de medirlos.
    `start_date`/`end_date` ya vienen validados por `symbology_window`, que
    garantiza `start < end` y fin no futuro.
    """
    result = client.symbology.resolve(
        "GLBX.MDP3",
        list(symbols),
        stype_in=stype_in,
        stype_out="instrument_id",
        start_date=start_date,
        end_date=end_date,
    )["result"]
    return {
        sym: int(entries[0]["s"])
        for sym, entries in result.items()
        if entries and entries[0].get("s")
    }


def expand_root(root: str, start_date: str) -> List[str]:
    """Todos los `<root><mes><año>` posibles alrededor de `start_date`. Puro.

    Tres años naturales (anterior, actual, siguiente) alcanzan porque un
    contrato vive menos de un año; con más solo se piden símbolos que van a
    devolver "no existe" y se pagan llamadas de más.
    """
    if not root or root[-1].isdigit():
        raise ValueError(f"raíz de símbolo no reconocida: {root!r} (esperado algo como '6E')")
    base_year = int(datetime.strptime(start_date, "%Y-%m-%d").year)
    years = [str(y)[-1] for y in range(base_year - 1, base_year + 2)]
    return [f"{root}{m}{y}" for y in years for m in MONTH_CODES]


def resolve_months(client: Any, root: str, start_date: str, end_date: str) -> Dict[str, int]:
    """Resuelve todos los meses vivos del subyacente `root` (p. ej. "6E")."""
    return resolve_symbols(client, expand_root(root, start_date), start_date, end_date)


def measure_counts(
    client: Any,
    symbols: Sequence[str],
    start: str,
    end: str,
    stype_in: str = "raw_symbol",
) -> Dict[str, int]:
    """Trades por símbolo en la ventana. Solo metadata: no descarga ticks."""
    counts: Dict[str, int] = {}
    unresolvable: List[str] = []
    for sym in symbols:
        try:
            counts[sym] = int(
                client.metadata.get_record_count(
                    "GLBX.MDP3",
                    start,
                    end,
                    symbols=[sym],
                    schema=SCHEMA,
                    stype_in=stype_in,
                )
            )
        except Exception:  # noqa: BLE001 - un mes raro no aborta la medición
            # Contrato que la simbología da como válido pero que no llega a
            # cotizar en la ventana de datos: cuenta como 0 (no está vivo) y no
            # como error. Se agrupa en UNA línea porque pedirle los 36 meses
            # posibles produce una pantalla de ruido con solo 15 interesantes.
            counts[sym] = 0
            unresolvable.append(sym)
    if unresolvable:
        print(
            f"[nota] {len(unresolvable)} símbolos sin cotizar en la ventana "
            f"(vencidos o aún no listados): {', '.join(sorted(unresolvable))}",
            file=sys.stderr,
        )
    return counts


def estimate_cost(
    client: Any, symbol: str, start: str, end: str, stype_in: str
) -> Optional[float]:
    """Coste en USD de la descarga, o None si el endpoint no está disponible.

    Es un guardia económico y no una formalidad: un rango de mes entero de MBO
    cuesta euros y este script no debe poder gastarlos sin que alguien lo vea.
    """
    try:
        return float(
            client.metadata.get_cost(
                "GLBX.MDP3",
                start,
                end,
                symbols=[symbol],
                schema=SCHEMA,
                stype_in=stype_in,
            )
        )
    except Exception as exc:  # noqa: BLE001 - se informa y se sigue, no se finge 0
        print(f"  [aviso] no se pudo estimar el coste: {exc}", file=sys.stderr)
        return None


def warn_if_thin(
    client: Any,
    root: str,
    symbol: str,
    trade_count: int,
    start: str,
    end: str,
    start_date: str,
    end_date: str,
    threshold: int = 1000,
) -> None:
    """Si el símbolo está vacío, dice cuál estaba lleno. Nunca decide solo.

    Es el fallo silencioso que motivó la selección automática: `6E.c.0` bajó 44
    trades donde `6EZ6` tenía 14.660, y el download devolvía 200 sin que nada
    pareciera mal. Un umbral de 1000 no es una verdad del mercado, es la señal
    de que merece la pena mirar; por eso solo avisa y muestra la alternativa.
    """
    if trade_count >= threshold:
        return
    print(
        f"[aviso] {symbol} solo tiene {trade_count} trades en la ventana "
        f"(umbral {threshold}): parece un contrato sin liquidez.",
        file=sys.stderr,
    )
    if ".c." in root:
        root = root.split(".c.")[0]
    try:
        counts = measure_counts(
            client, sorted(resolve_months(client, root, start_date, end_date)), start, end
        )
        best = select_symbol(counts)
    except Exception as exc:  # noqa: BLE001 - el aviso no puede tumbar la bajada
        print(f"  [aviso] no se pudo comparar con los demás meses: {exc}", file=sys.stderr)
        return
    if best != symbol:
        print(
            f"  [aviso] el más líquido de {root} en esa ventana es {best} "
            f"con {counts[best]} trades; use --symbol {best}.",
            file=sys.stderr,
        )


# --- Descarga ----------------------------------------------------------------


def download(
    client: Any, symbol: str, start: str, end: str, stype_in: str, dest: Path
) -> Any:
    """Baja la cinta y la escribe en Parquet. Devuelve el DBNStore leído."""
    ensure_dir(str(dest.parent))
    store = client.timeseries.get_range(
        "GLBX.MDP3",
        start,
        end,
        symbols=[symbol],
        schema=SCHEMA,
        stype_in=stype_in,
    )
    store.to_parquet(str(dest))
    return store


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sidecar(parquet: Path, payload: Dict[str, Any]) -> Path:
    """Procedencia junto al Parquet: qué se bajó, cuándo, cuánto costó y el hash.

    Sin esto no se puede responder "¿de dónde salió este número?" dentro de seis
    meses, y el fixture de calibración pierde toda su evidencia.
    """
    sidecar = parquet.with_suffix(parquet.suffix + ".meta.json")
    payload = dict(payload)
    payload.setdefault("parquet", parquet.name)
    payload["sha256"] = sha256_of(parquet)
    with open(sidecar, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    return sidecar


# --- CLI ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Descarga cinta real de Databento para calibrar el order flow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--start", required=True, help="2026-10-05 o 2026-10-05T13:30:00")
    parser.add_argument(
        "--end",
        required=True,
        help="EXCLUSIVO: 2026-10-08 cubre hasta el final del día 7",
    )
    parser.add_argument(
        "--symbol",
        default="6E",
        help="Contrato concreto (6EZ6), continuous (6E.c.0) o raíz con selección "
        "automática del más líquido (default: 6E)",
    )
    parser.add_argument("--jsonl", default=None, help="Ruta del .jsonl de salida")
    parser.add_argument("--limit", type=int, default=0, help="Máx. líneas del .jsonl")
    parser.add_argument(
        "--max-cost",
        type=float,
        default=1.0,
        help="USD máx. estimados; por encima, aborta (default 1.0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resuelve, mide y estima el coste sin descargar nada",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    fix_console_encoding()
    args = build_parser().parse_args(argv)

    env_file = load_env()
    start = parse_instant(args.start)
    end = parse_instant(args.end, end=True)
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    if end_dt <= start_dt:
        raise SystemExit(f"--end ({end}) tiene que ser posterior a --start ({start})")

    # Databento no guarda datos futuros y lo dice con un error que menciona
    # licencias en vez de fechas. Se corta aquí, con el motivo correcto.
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    if end_dt > now_utc:
        raise SystemExit(
            f"--end {end} está por delante de ahora ({now_utc.isoformat(timespec='seconds')}Z): "
            "no hay datos futuros. Databento además publica con unos minutos de "
            "retraso, así que acorte el rango un par de minutos por debajo."
        )

    # El guardián decide, no un comentario. Si apunta a OneDrive o al repo,
    # aquí se aborta ANTES de crear ni un byte.
    for target in (DATABENTO_RAW_DIR,):
        problem = data_root_guard_error(target)
        if problem:
            raise SystemExit(f"raíz de datos inválida ({target}): {problem}")

    client = _client()
    start_date, end_date = symbology_window(start, end)

    kind = classify_symbol(args.symbol)
    stype_in = "continuous" if kind == "continuous" else "raw_symbol"
    instrument_id = None
    trade_count: Optional[int] = None
    symbol = args.symbol

    if kind == "continuous":
        resolved = resolve_symbols(
            client, [symbol], start_date, end_date, stype_in="continuous"
        )
        if symbol not in resolved:
            raise SystemExit(f"{symbol}: no resolvió en {start_date}..{end_date}")
        instrument_id = resolved[symbol]
        trade_count = measure_counts(
            client, [symbol], start, end, stype_in="continuous"
        )[symbol]
        print(f"continuous {symbol} -> instrument_id {instrument_id}")
    elif kind == "contract":
        resolved = resolve_symbols(client, [symbol], start_date, end_date)
        if symbol not in resolved:
            raise SystemExit(
                f"{symbol}: no resuelve en {start_date}..{end_date}; "
                "contrato vencido o mal escrito. Pase una raíz (6E) y el script "
                "mira qué meses están vivos."
            )
        instrument_id = resolved[symbol]
        trade_count = measure_counts(client, [symbol], start, end)[symbol]
    else:
        months = resolve_months(client, symbol, start_date, end_date)
        if not months:
            raise SystemExit(
                f"{symbol}: ningún mes resolvió en {start_date}..{end_date}"
            )
        counts = measure_counts(client, sorted(months), start, end)
        symbol = select_symbol(counts)
        instrument_id = months[symbol]
        trade_count = counts[symbol]
        silent = sorted(s for s in months if counts.get(s, 0) == 0)
        note = f" (sin liquidez: {', '.join(silent)})" if silent else ""
        print(f"seleccionado {symbol} de {len(months)} meses: {trade_count} trades{note}")

    if trade_count is not None:
        warn_if_thin(client, args.symbol, symbol, trade_count, start, end, start_date, end_date)

    cost = estimate_cost(client, symbol, start, end, stype_in)
    if cost is not None:
        print(f"coste estimado: {cost:.4f} USD")
        if cost > args.max_cost:
            raise SystemExit(
                f"coste {cost:.4f} USD > --max-cost {args.max_cost}: "
                "suba el límite a sabiendas o acorte el rango"
            )
    if args.dry_run:
        print(f"dry-run OK: {symbol} ({stype_in}) {start}..{end}")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = symbol.replace(".", "_")
    parquet = Path(DATABENTO_RAW_DIR) / (
        f"GLBX_{safe}_{SCHEMA}_{start[:10]}_{end[:10]}_{stamp}.parquet"
    )
    store = download(client, symbol, start, end, stype_in, parquet)
    rows = list(store.to_df().to_dict("records"))
    print(f"parquet: {parquet} ({parquet.stat().st_size / 1024:.1f} KiB, {len(rows)} trades)")

    jsonl_rows = 0
    jsonl_path = Path(args.jsonl) if args.jsonl else None
    if jsonl_path is not None:
        if not jsonl_path.is_absolute():
            jsonl_path = Path(PROJECT_DIR) / jsonl_path
        jsonl_rows = write_jsonl(rows, jsonl_path, limit=args.limit)
        print(f"jsonl:   {jsonl_path} ({jsonl_rows} trades)")

    sidecar = write_sidecar(
        parquet,
        {
            "dataset": "GLBX.MDP3",
            "schema": SCHEMA,
            "requested_symbol": args.symbol,
            "symbol": symbol,
            "instrument_id": instrument_id,
            "stype_in": stype_in,
            "start": start,
            "end": end,
            "rows": len(rows),
            "trades_in_window": trade_count,
            "cost_usd": cost,
            "databento_api_key_present": bool(os.environ.get("DATABENTO_API_KEY")),
            "env_file": str(env_file) if env_file else None,
            "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
            "jsonl": str(jsonl_path) if jsonl_path else None,
            "jsonl_rows": jsonl_rows,
        },
    )
    print(f"sidecar: {sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
