"""La estrategia de trading, materializada y validada. Parte PURA.

`config/strategy.yaml` es la fuente única de verdad de las reglas. Este módulo es la
mitad pura de leerlo: recibe el documento ya leído (un `dict`), lo valida clave por
clave y devuelve la config plana de siempre —el shape que consumen la API, el
agente y `core/risk_engine`— sin tocar el disco.

La otra mitad (leer, cachear y escribir el fichero) vive en
`settings/strategy_source.py`. La separación no es purismo: `tests/unit/
test_core_purity.py` prohíbe `open()` dentro de `core/`, y con razón. Un módulo que
abre un fichero en cada llamada convierte cada test unitario de este archivo en un
test de integración que depende del disco, y el `strategy.yaml` de producción es
justo el fichero que un test no debe poder pisar.

Normas de validación (las de REF, y por el mismo motivo):
  - Una clave desconocida se ignora, para no petar el arranque por reglas nuevas.
  - Un valor con el tipo incorrecto o fuera de rango LANZA `StrategyConfigError`,
    para que el plan nunca se ejecute con reglas rotas en silencio.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from core import risk_engine

#: Suelo del umbral de score, en puntos. NO es la calibración de producción (esa
#: está en `strategy.yaml`: 59.5); es lo que se usa si la clave no está. Existe como
#: constante y no como literal en varios sitios porque `cfg.get("min_score") or 80.0`
#: confunde "no está" con "es 0.0", y 0.0 es un valor legal (el rango validado es
#: 0..100): un umbral pedido explícitamente en 0 se sustituía en silencio por 80.
DEFAULT_MIN_SCORE: float = 80.0

# Clave legacy: distancia de SL expresada en pips. Se LEE (para migrar configs
# viejos con la ayuda del spec del símbolo) pero no se escribe ni se valida: el
# contrato del sistema es la distancia absoluta, en unidades de precio.
LEGACY_SL_PIPS_KEY = "sl_default_pips"


class StrategyConfigError(ValueError):
    """`strategy.yaml` no es utilizable: falta, no es un mapeo o tiene una regla rota.

    Hereda de `ValueError` porque es lo que lanzaba REF y porque quien la captura
    para degradar a "sin reglas" (`agent.laya_bridge`, `api.deps.resumen_config`)
    sigue funcionando sin cambiar. Lo que NO puede heredar es el significado HTTP:
    un YAML roto es el servidor mal configurado, no una petición mala, así que
    `api/errors.py` lo registra aparte y devuelve 503 en vez del 400 genérico de
    `ValueError`.
    """


# ============================================================
# Valores por defecto (comportamiento histórico si el YAML no dice nada)
# ============================================================

DEFAULTS: Dict[str, Any] = {
    # Perfil de riesgo
    "risk_pct": 0.5,
    "reduced_risk_pct": 0.25,
    "loss_limit_fixed": 1250.0,
    "loss_limit_pct": 2.0,
    "max_trades_day": 10,
    # Ejecución
    "magic": 8882026,
    "comment": "Web Exec",
    # Distancia de SL en UNIDADES DE PRECIO (no pips: un pip no significa lo mismo
    # en EURUSD que en oro). Es el contrato del sistema; el pip queda solo para
    # presentar en la UI. `sl_distance_by_symbol` manda sobre el valor global.
    "sl_distance": 0.00120,
    "sl_distance_by_symbol": {},
    "tp_ratio_r": 2.0,
    "deviation_points": 20,
    "news_buffer_min": 15,
    "symbols_allow": [],
    # Score.
    #
    # `min_score` y `risk_weights` se leen de sus módulos, no se duplican aquí: antes
    # este archivo, `risk_engine` y el YAML tenían tres juegos distintos de pesos y
    # cuatro umbrales, y ganaba el que se alcanzaba primero. Los de `risk_engine` son
    # el suelo estructural (ningún componente con feed universal puede valer 0, porque
    # un 0 ahí es un componente muerto). La calibración de producción —incluido
    # `killzone: 0.0`, que el watcher exige además como AND duro— vive solo en
    # `strategy.yaml`. Ver `min_score_for` y `risk_engine.DEFAULT_WEIGHTS`.
    "min_score": DEFAULT_MIN_SCORE,
    "min_rr": 2.0,
    "setup_ttl_minutes": 45,
    "risk_weights": dict(risk_engine.DEFAULT_WEIGHTS),
    "grade_thresholds": dict(risk_engine.DEFAULT_GRADE_THRESHOLDS),
    # Killzones (UTC), tomadas de `risk_engine` y no repetidas aquí: las dos copias
    # podían divergir y entonces el path de test y el real medirían ventanas
    # distintas. Ver `risk_engine.DEFAULT_KILLZONES`.
    "killzones": [dict(w) for w in risk_engine.DEFAULT_KILLZONES],
    # Reloj: zona del SERVIDOR DEL BROKER. Define el "día de trading" que comparten
    # el EA y el backend (contador diario, HWM y balance inicial). Lo consume
    # `core.clock`. Ver `clock:` en strategy.yaml.
    "broker_tz": "Europe/Helsinki",
    "broker_utc_offset_minutes": 0,
    # Prop firm
    "prop_enabled": False,
    "prop_max_dd_daily_pct": 2.0,
    "prop_max_dd_total_pct": 6.0,
    "prop_max_profit_day_pct": 1.5,
    "prop_consistency_days": 4,
    # Fuentes de datos
    "data_sources": {
        "cot": True,
        "cvd_of": True,
        "smc": True,
        "killzone": True,
        "news": True,
        "orderflow": False,
        "smr_dxy": True,
    },
    # Watcher (bot a la escucha)
    "watcher_config": {
        "enabled": True,
        "scan_interval_sec": 60,
        "auto_execute": False,
        "dedup_ttl_sec": 2700,
        "symbols": [{"symbol": "EURUSD", "timeframe": "M15"}],
    },
    "agent_topics": {
        "account": ["saldo", "equity", "margen", "balance", "capital", "cuenta", "account"],
        "positions": ["posicion", "abierta", "abierto", "trades"],
        "history": ["historial", "historia", "cerrada", "historico", "gan"],
        "news": ["noticia", "noticias", "economi", "calendario", "dato", "pip", "fed", "nipc", "inflacion"],
        "trade": ["oper", "entrar", "entrada", "setup", "compr", "vend", "buy", "sell",
                  "stoploss", "sl", "tp", "take", "riesgo", "score", "ganar", "perder"],
    },
    "agent_system_prompt": "",
    "agent_risk_policy": "",
    "agent_data_sources_policy": "",
    "agent_allowed_tools": [
        "account_info", "positions_list", "history", "price", "now",
        "orderflow_snapshot", "orderflow_alerts", "patterns",
        "trade_query", "journal_append", "journal_list",
        "mt5_export_read", "chart_snapshot", "economic_news",
        "set_chart_alert", "chart_annotate",
    ],
}

# ============================================================
# Especificación de tipos y rangos por clave, para la validación
# ============================================================
# (tipo, mínimo, máximo). El mínimo o el máximo a None es "sin límite".

_BOOLEAN = "bool"
_NUMBER = "number"
_STRING = "string"
_LIST_STR = "list_str"
_MAP_NUMBER = "map_number"


def _spec_for(key: str) -> Optional[Tuple[str, Optional[float], Optional[float]]]:
    """`(tipo, mínimo, máximo)` de una clave plana, o `None` si no se valida."""
    table: Dict[str, Tuple[str, Any, Any]] = {
        "risk_pct": (_NUMBER, 0.0, 100.0),
        "reduced_risk_pct": (_NUMBER, 0.0, 100.0),
        "loss_limit_fixed": (_NUMBER, 0.0, None),
        "loss_limit_pct": (_NUMBER, 0.0, 100.0),
        "max_trades_day": (_NUMBER, 0, None),
        "magic": (_NUMBER, 0, None),
        "comment": (_STRING, None, None),
        "sl_distance": (_NUMBER, 0.0, None),
        "sl_distance_by_symbol": (_MAP_NUMBER, None, None),
        "tp_ratio_r": (_NUMBER, 0.0, None),
        "deviation_points": (_NUMBER, 0, None),
        "news_buffer_min": (_NUMBER, 0, None),
        "symbols_allow": (_LIST_STR, None, None),
        "min_score": (_NUMBER, 0.0, 100.0),
        "min_rr": (_NUMBER, 0.0, None),
        "setup_ttl_minutes": (_NUMBER, 0, None),
        "prop_max_dd_daily_pct": (_NUMBER, 0.0, 100.0),
        "prop_max_dd_total_pct": (_NUMBER, 0.0, 100.0),
        "prop_max_profit_day_pct": (_NUMBER, 0.0, 100.0),
        "prop_consistency_days": (_NUMBER, 0, None),
        "broker_tz": (_STRING, None, None),
        "broker_utc_offset_minutes": (_NUMBER, -1440, 1440),
    }
    return table.get(key)


def _check(value: Any, spec: Tuple[str, Any, Any], key: str) -> None:
    """Valida un valor contra su spec. Lanza `StrategyConfigError` si no cuadra."""
    kind, lo, hi = spec
    if kind == _MAP_NUMBER:
        if not isinstance(value, dict):
            raise StrategyConfigError(
                f"strategy.yaml: '{key}' debe ser un objeto {{SÍMBOL: distancia}}."
            )
        for sym, dist in value.items():
            if not isinstance(sym, str) or not sym.strip():
                raise StrategyConfigError(
                    f"strategy.yaml: '{key}' tiene una clave de símbolo vacía."
                )
            if isinstance(dist, bool) or not isinstance(dist, (int, float)) or dist <= 0:
                raise StrategyConfigError(
                    f"strategy.yaml: '{key}.{sym}' debe ser una distancia > 0 en precio "
                    f"(es {dist})."
                )
        return
    if kind == _BOOLEAN:
        if not isinstance(value, bool):
            raise StrategyConfigError(
                f"strategy.yaml: '{key}' debe ser true/false (booleano)."
            )
        return
    if kind == _STRING:
        if not isinstance(value, str):
            raise StrategyConfigError(f"strategy.yaml: '{key}' debe ser un texto.")
        return
    if kind == _LIST_STR:
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise StrategyConfigError(
                f"strategy.yaml: '{key}' debe ser una lista de símbolos/textos."
            )
        return
    if kind == _NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StrategyConfigError(f"strategy.yaml: '{key}' debe ser un número.")
        if lo is not None and value < lo:
            raise StrategyConfigError(
                f"strategy.yaml: '{key}' no puede ser menor que {lo} (es {value})."
            )
        if hi is not None and value > hi:
            raise StrategyConfigError(
                f"strategy.yaml: '{key}' no puede ser mayor que {hi} (es {value})."
            )


# ============================================================
# Parseo de las secciones no planas
# ============================================================

def _parse_killzones(raw: Any) -> List[Dict[str, str]]:
    """Ventanas UTC aplanadas. Acepta el formato plano (lista) y el jerárquico
    `{timezone, windows: [...]}`.

    La zona horaria informativa no se aplana: los consumidores trabajan siempre en
    UTC (`risk_engine.killzone_score`), y aplicarle un offset aquí cambiaría las
    ventanas sin que nadie lo pidiera.
    """
    if raw is None:
        return [dict(w) for w in DEFAULTS["killzones"]]
    if isinstance(raw, dict):
        raw = raw.get("windows")
        if not isinstance(raw, list):
            raise StrategyConfigError(
                "strategy.yaml: 'killzones' debe ser una lista (formato plano) o "
                "{timezone, windows: [...]} (formato jerárquico)."
            )
    if not isinstance(raw, list):
        raise StrategyConfigError("strategy.yaml: 'killzones' debe ser una lista de ventanas.")
    out: List[Dict[str, str]] = []
    for w in raw:
        if not isinstance(w, dict) or "start" not in w or "end" not in w:
            raise StrategyConfigError(
                "strategy.yaml: cada killzone debe tener 'name', 'start' y 'end' HH:MM."
            )
        for k in ("start", "end"):
            if not isinstance(w.get(k), str) or ":" not in w[k]:
                raise StrategyConfigError(
                    f"strategy.yaml: killzone '{w.get('name', '?')}' requiere "
                    f"'{k}' en formato HH:MM."
                )
        out.append({
            "name": str(w.get("name") or "?"),
            "start": str(w["start"]),
            "end": str(w["end"]),
        })
    return out if out else [dict(w) for w in DEFAULTS["killzones"]]


def min_score_for(cfg: Dict[str, Any]) -> float:
    """El umbral de score resuelto desde la config, en un solo sitio.

    Distingue "no configurado" de "configurado a 0.0", que es un valor legal (el
    rango validado es 0..100). El patrón anterior
    `float(cfg.get("min_score") or 80.0)` no podía: un 0 explícito se caía al `or`
    y se ejecutaba con 80 sin que nada lo dijera.
    """
    raw = (cfg or {}).get("min_score")
    if raw is None or isinstance(raw, bool):
        return DEFAULT_MIN_SCORE
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_SCORE


def _json_o_estructura(value: Any) -> Any:
    """La config plana guarda dos claves como JSON string; se Deserializa.

    `risk_weights` y `killzones` salen de `build_flat` como `json.dumps(...)` porque
    son mapas y listas que un escalar en línea no puede expresar, y ese es el shape
    que consumían el monolito, la UI y el agente. Quien las lee tiene que aceptar las
    DOS formas: el string (que es lo que sale de aquí y de la base) y la estructura ya
    parseada (un test, un doble, o un consumidor futuro que prefiera no serializar).
    Aceptar solo una hace que la otra ruta falle, y con un error que no parece de
    configuración: `killzone_score` iterating un string levanta `AttributeError`
    ('str' no tiene 'get'), el score entero revienta y el panel se queda sin veredicto
    sin decir por qué.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return value


def _primer_valor(cfg: Optional[Dict[str, Any]], *caminos: Any) -> Any:
    """El primer valor de `caminos` que exista y no esté vacío.

    Se aceptan las dos formas porque las dos EXISTEN en este repositorio: la plana que
    produce `build_flat` (y que consume el store, la UI y el agente) y la anidada del
    `strategy.yaml` tal cual, que es lo que llevan `BrokerClock` y los tests. Aceptar
    solo una deja el otro camino con los defaults en silencio, y un default silencioso
    es indistinguible de una decisión.
    """
    c = cfg or {}
    for camino in caminos:
        valor: Any = c
        for clave in camino:
            if not isinstance(valor, dict):
                valor = None
                break
            valor = valor.get(clave)
        if valor is None or valor == "" or valor == {} or valor == []:
            continue
        return valor
    return None


def risk_weights_for(cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, float]]:
    """Los pesos del score desde la config, o `None` si no hay ninguno utilizable.

    Acepta `risk_weights` (plano, como string JSON o ya parseado) y `score.weights`
    (anidado, el `strategy.yaml` sin aplanar).

    `None` NO es lo mismo que `{}`: `None` deja que `risk_engine` aplique sus
    defaults, y `{}` haría lo mismo pero fingiendo que la configuración dijo algo. La
    diferencia es la que permite a quien llama avisar de que está midiendo con la
    calibración de fábrica en vez de con la del YAML.
    """
    pesos = _json_o_estructura(_primer_valor(cfg, ("risk_weights",), ("score", "weights")))
    if not isinstance(pesos, dict):
        return None
    salida: Dict[str, float] = {}
    for clave, valor in pesos.items():
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            continue
        salida[str(clave)] = float(valor)
    return salida or None



def killzones_for(cfg: Optional[Dict[str, Any]] = None) -> Optional[List[Dict[str, str]]]:
    """Las ventanas de killzone desde la config, o `None` si no hay ninguna.

    Acepta las dos formas, como `risk_weights_for`: la plana (`killzones`, que
    `build_flat` serializa a JSON string) y la del `strategy.yaml` sin aplanar. También
    acepta `{"windows": [...]}` porque esa es la forma que trae el bloque de sesiones
    cuando se guarda agrupado.

    Igual que los pesos: `None` significa "usa `risk_engine.DEFAULT_KILLZONES`", y quien
    llama lo declara en vez de dejar que el score parezca medido con la configuración
    real cuando no lo está.
    """
    ventanas = _primer_valor(cfg, ("killzones",), ("score", "killzones"))
    ventanas = _json_o_estructura(ventanas)
    if isinstance(ventanas, dict):
        ventanas = ventanas.get("windows")
    if not isinstance(ventanas, list):
        return None
    salida: List[Dict[str, str]] = []
    for w in ventanas:
        if not isinstance(w, dict):
            continue
        inicio, fin = w.get("start"), w.get("end")
        if not isinstance(inicio, str) or not isinstance(fin, str):
            continue
        if ":" not in inicio or ":" not in fin:
            continue
        salida.append({"name": str(w.get("name") or "?"), "start": inicio, "end": fin})
    return salida or None



def _parse_weights(raw: Any, key: str = "score.weights") -> Dict[str, float]:
    """Pesos del score, con los de `risk_engine` como base y solo las claves conocidas."""
    if raw is None:
        return dict(DEFAULTS["risk_weights"])
    if not isinstance(raw, dict):
        raise StrategyConfigError(f"strategy.yaml: '{key}' debe ser un objeto de pesos.")
    base = dict(DEFAULTS["risk_weights"])
    for k in ("cot", "cvd_of", "smc", "killzone", "smr_dxy"):
        if k in raw:
            v = raw[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise StrategyConfigError(
                    f"strategy.yaml: 'weights.{k}' debe ser un número >= 0."
                )
            base[k] = float(v)
    return base


def _parse_data_sources(raw: Any) -> Dict[str, bool]:
    """Flags de fuentes de datos: solo los conocidos, y obligatoriamente booleanos."""
    if raw is None:
        return dict(DEFAULTS["data_sources"])
    if not isinstance(raw, dict):
        raise StrategyConfigError(
            "strategy.yaml: 'data_sources' debe ser un objeto de flags true/false."
        )
    out = dict(DEFAULTS["data_sources"])
    for k in out:
        if k in raw:
            if not isinstance(raw[k], bool):
                raise StrategyConfigError(
                    f"strategy.yaml: 'data_sources.{k}' debe ser true/false."
                )
            out[k] = raw[k]
    return out


def _parse_topics(raw: Any) -> Dict[str, List[str]]:
    """Palabras clave del agente por tema. Las claves desconocidas se ignoran."""
    if raw is None:
        return {k: list(v) for k, v in DEFAULTS["agent_topics"].items()}
    if not isinstance(raw, dict):
        raise StrategyConfigError(
            "strategy.yaml: 'agent.topics' debe ser un objeto de palabras clave."
        )
    out: Dict[str, List[str]] = {}
    for key, default in DEFAULTS["agent_topics"].items():
        val = raw.get(key, default)
        if not isinstance(val, list) or not all(isinstance(x, str) and x for x in val):
            raise StrategyConfigError(
                f"strategy.yaml: 'agent.topics.{key}' debe ser una lista de palabras."
            )
        out[key] = list(val)
    return out


def _parse_watcher(raw: Any) -> Dict[str, Any]:
    """Valida la sección `watcher`. Acepta sección ausente (defaults)."""
    if raw is None:
        return {
            "enabled": True,
            "scan_interval_sec": 60,
            "auto_execute": False,
            "dedup_ttl_sec": 2700,
            "symbols": [{"symbol": "EURUSD", "timeframe": "M15"}],
        }
    if not isinstance(raw, dict):
        raise StrategyConfigError("strategy.yaml: 'watcher' debe ser un objeto.")

    def _bool(val: Any, key: str) -> bool:
        if not isinstance(val, bool):
            raise StrategyConfigError(f"strategy.yaml: 'watcher.{key}' debe ser true/false.")
        return val

    def _num(val: Any, key: str) -> float:
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
            raise StrategyConfigError(
                f"strategy.yaml: 'watcher.{key}' debe ser un número >= 0."
            )
        return float(val)

    out: Dict[str, Any] = {
        "enabled": _bool(raw.get("enabled", True), "enabled"),
        "scan_interval_sec": _num(raw.get("scan_interval_sec", 60), "scan_interval_sec"),
        "auto_execute": _bool(raw.get("auto_execute", False), "auto_execute"),
        "dedup_ttl_sec": _num(raw.get("dedup_ttl_sec", 2700), "dedup_ttl_sec"),
    }
    syms_raw = raw.get("symbols")
    if syms_raw is None:
        out["symbols"] = [{"symbol": "EURUSD", "timeframe": "M15"}]
        return out
    if not isinstance(syms_raw, list) or not syms_raw:
        raise StrategyConfigError(
            "strategy.yaml: 'watcher.symbols' debe ser una lista no vacía."
        )
    syms = []
    for s in syms_raw:
        if not isinstance(s, dict):
            raise StrategyConfigError(
                "strategy.yaml: cada 'watcher.symbols' debe tener symbol y timeframe."
            )
        symbol = str(s.get("symbol") or "EURUSD").upper()
        timeframe = str(s.get("timeframe") or "M15").upper()
        if not symbol:
            raise StrategyConfigError(
                "strategy.yaml: 'watcher.symbols[].symbol' no puede estar vacío."
            )
        syms.append({"symbol": symbol, "timeframe": timeframe})
    out["symbols"] = syms
    return out


# ============================================================
# El YAML jerárquico -> la config plana de siempre
# ============================================================

def build_flat(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Convierte el YAML jerárquico en la config plana del shape histórico.

    `risk_weights` y `killzones` salen como JSON string porque ese es el shape que
    los consumidores de REF_expectan (`json.loads(...)`); mantenerlo evita tocar
    `risk_engine`, el agente y la UI a la vez.
    """
    d = doc.get("risk") or {}
    e = doc.get("execution") or {}
    s = doc.get("score") or {}
    prop = doc.get("prop") or {}
    agent = doc.get("agent") or {}
    clock = doc.get("clock") or {}

    flat: Dict[str, Any] = dict(DEFAULTS)

    scalar = {
        "risk_pct": ("risk", "risk_pct"),
        "reduced_risk_pct": ("risk", "reduced_risk_pct"),
        "loss_limit_fixed": ("risk", "loss_limit_fixed"),
        "loss_limit_pct": ("risk", "loss_limit_pct"),
        "max_trades_day": ("risk", "max_trades_day"),
        "magic": ("execution", "magic"),
        "comment": ("execution", "comment"),
        "sl_distance": ("execution", "sl_distance"),
        "sl_distance_by_symbol": ("execution", "sl_distance_by_symbol"),
        "tp_ratio_r": ("execution", "tp_ratio_r"),
        "deviation_points": ("execution", "deviation_points"),
        "news_buffer_min": ("execution", "news_buffer_min"),
        "symbols_allow": ("execution", "symbols_allow"),
        "min_score": ("score", "min_score"),
        "min_rr": ("score", "min_rr"),
        "setup_ttl_minutes": ("score", "setup_ttl_minutes"),
        "prop_enabled": ("prop", "enabled"),
        "prop_max_dd_daily_pct": ("prop", "max_dd_daily_pct"),
        "prop_max_dd_total_pct": ("prop", "max_dd_total_pct"),
        "prop_max_profit_day_pct": ("prop", "max_profit_day_pct"),
        "prop_consistency_days": ("prop", "consistency_days"),
        "broker_tz": ("clock", "broker_tz"),
        "broker_utc_offset_minutes": ("clock", "broker_utc_offset_minutes"),
    }
    secs = {"risk": d, "execution": e, "score": s, "prop": prop, "clock": clock}
    for key, (sec, k) in scalar.items():
        src = secs[sec]
        if k in src:
            flat[key] = src[k]

    gth = s.get("grade_thresholds") or DEFAULTS["grade_thresholds"]
    if isinstance(gth, dict):
        flat["grade_thresholds"] = gth

    flat["risk_weights"] = json.dumps(
        _parse_weights(s.get("weights"), "score.weights"), ensure_ascii=False
    )
    flat["killzones"] = json.dumps(
        _parse_killzones(doc.get("killzones")), ensure_ascii=False
    )
    flat["data_sources"] = _parse_data_sources(doc.get("data_sources"))
    flat["watcher_config"] = _parse_watcher(doc.get("watcher"))
    flat["agent_topics"] = _parse_topics(agent.get("topics"))
    # Textos del agente (prompt / policy), leídos de su sección del YAML.
    flat["agent_system_prompt"] = str(agent.get("system_prompt") or "").strip()
    flat["agent_risk_policy"] = str(agent.get("risk_policy") or "").strip()
    flat["agent_data_sources_policy"] = str(agent.get("data_sources_policy") or "").strip()
    flat["agent_allowed_tools"] = (
        agent.get("allowed_tools") or list(DEFAULTS["agent_allowed_tools"])
    )

    for key in scalar:
        spec = _spec_for(key)
        if spec:
            _check(flat[key], spec, key)
    # `prop_enabled` es booleano y por eso no pasa por el validador numérico.
    if not isinstance(flat.get("prop_enabled"), bool):
        raise StrategyConfigError(
            "strategy.yaml: 'prop.enabled' debe ser true/false (booleano)."
        )

    return flat


def load_doc(text: str) -> Dict[str, Any]:
    """Texto YAML -> documento. Un fichero vacío es un documento vacío, no un fallo.

    Lanzar `StrategyConfigError` (y no el `yaml.YAMLError` de PyYAML) para que quien
    carga la config tenga una sola excepción que capturar, venga el fallo de donde
    venga: sintaxis, tipo o rango.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StrategyConfigError(f"strategy.yaml no es un YAML válido: {exc}") from exc
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise StrategyConfigError("strategy.yaml: la raíz debe ser un objeto.")
    return doc


def dump_yaml(doc: Dict[str, Any]) -> str:
    """Documento -> texto YAML.

    Se usa SOLO para las claves que un escalar en línea no puede representar
    (`risk_weights`, `killzones`, `sl_distance_by_symbol`). Para eso se pierde el
    resto de comentarios del fichero, y por eso `patch_text` es el camino
    normal: reescribir el archivo entero por cambiar un número es la forma rápida de
    perder la documentación de las reglas.
    """
    return yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ============================================================
# Escritura: parche surgical del texto
# ============================================================

#: `(clave plana) -> (sección YAML, subclave)`. El orden NO es el del YAML: es el
#: grupo de ejecución primero, porque es el que la UI edita entero.
FLAT_TO_YAML: Dict[str, Tuple[Optional[str], Optional[str]]] = {
    "risk_pct": ("risk", "risk_pct"),
    "reduced_risk_pct": ("risk", "reduced_risk_pct"),
    "loss_limit_fixed": ("risk", "loss_limit_fixed"),
    "loss_limit_pct": ("risk", "loss_limit_pct"),
    "max_trades_day": ("risk", "max_trades_day"),
    "magic": ("execution", "magic"),
    "comment": ("execution", "comment"),
    "sl_distance": ("execution", "sl_distance"),
    "sl_distance_by_symbol": ("execution", "sl_distance_by_symbol"),
    "tp_ratio_r": ("execution", "tp_ratio_r"),
    "deviation_points": ("execution", "deviation_points"),
    "news_buffer_min": ("execution", "news_buffer_min"),
    "symbols_allow": ("execution", "symbols_allow"),
    "min_score": ("score", "min_score"),
    "min_rr": ("score", "min_rr"),
    "setup_ttl_minutes": ("score", "setup_ttl_minutes"),
    "prop_enabled": ("prop", "enabled"),
    "prop_max_dd_daily_pct": ("prop", "max_dd_daily_pct"),
    "prop_max_dd_total_pct": ("prop", "max_dd_total_pct"),
    "prop_max_profit_day_pct": ("prop", "max_profit_day_pct"),
    "prop_consistency_days": ("prop", "consistency_days"),
    "risk_weights": ("score", "weights"),
    "killzones": (None, None),  # sección toplevel: tratamiento especial
}


def apply_updates(doc: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Aplica actualizaciones PLANAS al documento jerárquico, en memoria.

    Se usa junto a `dump_yaml` (y no en su lugar) para el camino de reescritura
    completa. Las claves desconocidas se ignoran y las que no tienen sección se
    saltan: aquí no hay dónde validarlas, y soltar un `ValueError` por una clave
    que el YAML ni siquiera tiene convertiría un guardado válido en un error.
    """
    for key, value in updates.items():
        if key in ("risk_weights", "killzones"):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    continue
            if key == "risk_weights":
                doc.setdefault("score", {})
                doc["score"]["weights"] = value
            else:
                doc["killzones"] = value
            continue

        mapping = FLAT_TO_YAML.get(key)
        if not mapping:
            continue
        section, subkey = mapping
        if section is None or subkey is None:
            continue
        doc.setdefault(section, {})
        doc[section][subkey] = value
    return doc


def yaml_scalar(value: Any) -> str:
    """Serializa un escalar o una lista plana como YAML compacto.

    Compacto y sin el marcador `...` que añade `yaml.dump`: aquí el texto va dentro
    de una línea que ya existe en el fichero, y un bloque de tres líneas debajo
    dejaría la clave duplicada.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        items = ", ".join(yaml_scalar(x) for x in value)
        return f"[{items}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = ", ".join(f"{yaml_plain_key(k)}: {yaml_scalar(v)}" for k, v in value.items())
        return "{" + (items[:80] + ",\n  ..." if len(items) > 80 else items) + "}"
    s = str(value)
    if s == "":
        return '""'
    # Plain (bare) solo si es seguro: alfanum y separadores, sin espacios.
    if re.fullmatch(r"[A-Za-z0-9_./+\-]+", s):
        return s
    quoted = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + quoted.replace("\n", "\\n") + '"'


def yaml_plain_key(key: Any) -> str:
    """Una clave de YAML en plano si se puede, entre comillas si no."""
    if re.fullmatch(r"[A-Za-z0-9_\-]+", str(key)):
        return str(key)
    return yaml_scalar(str(key))


def patch_text(text: str, section: str, subkey: str, value: Any) -> str:
    """Reemplaza `subkey:` dentro de la sección toplevel `section:`.

    Solo se reescribe la línea cuyo valor cambia: los comentarios —inline y de bloque—
    y el resto del archivo se quedan como están, que es lo que hace legible el YAML.
    Si la sección no existe se añade al final; si el subkey no existe, se inserta
    justo debajo de la cabecera de su sección.
    """
    lines = text.splitlines()
    sec_idx = None
    for i, ln in enumerate(lines):
        if not ln.strip().startswith("#") and ln.strip() == f"{section}:":
            sec_idx = i
            break
    if sec_idx is None:
        lines.append(f"{section}:\n  {subkey}: {yaml_scalar(value)}")
        return "\n".join(lines)

    key_pat = re.compile(r"^(\s*)" + re.escape(subkey) + r":(?=\s|$)")
    repl = yaml_scalar(value)
    for j in range(sec_idx + 1, len(lines)):
        ln = lines[j]
        if not ln.strip().startswith("#") and is_toplevel(ln):
            break
        m = key_pat.match(ln)
        if m and not ln.strip().startswith("#"):
            # El comentario inline de la línea original se conserva.
            inline = ""
            cm = re.search(r"(?<![\w])#", ln[m.end():])
            if cm:
                inline = ln[m.end() + cm.start():].rstrip()
            new_line = f"{m.group(1)}{subkey}: {repl}"
            if inline:
                # Alinear tras el valor para que siga leyéndose.
                pad = max(1, 22 - len(f"{subkey}: {repl}"))
                new_line += " " * pad + inline.lstrip()
            lines[j] = new_line
            return "\n".join(lines)

    # El subkey no existe aún: se inserta tras la cabecera de la sección.
    indent = "  "
    lines[sec_idx:sec_idx + 1] = [
        lines[sec_idx],
        f"{indent}{subkey}: {repl}",
    ]
    return "\n".join(lines)


def is_toplevel(line: str) -> bool:
    """¿La línea abre una clave toplevel (sin sangrar y con `:`)?"""
    return bool(line) and not line[0].isspace() and not line.strip().startswith("#") and ":" in line


# ============================================================
# Contrato de la distancia de SL (unidades de precio, NO pips)
# ============================================================
#
# Orden de resolución (el primero que exista gana):
#   1. `sl_distance_by_symbol[SYMBOL]` — la fuente de verdad por mercado.
#   2. `sl_distance` — valor global (un único número no puede ser correcto en dos
#      mercados a la vez; sirve de respaldo para el símbolo principal).
#   3. `sl_default_pips` (LEGACY) x `spec.pip` — puente para un YAML viejo. Sin spec
#      no se puede convertir, así que devuelve None: es preferible fallar en claro a
#      simular un stop 300 veces más pequeño en oro.

def sl_distance_for(cfg: Optional[Dict[str, Any]] = None,
                    symbol: Optional[str] = None,
                    spec: Any = None) -> Tuple[Optional[float], str]:
    """`(distancia, origen)` de la distancia de SL.

    El origen se propaga a la UI para que pueda avisar cuando se está usando el
    valor GLOBAL en un símbolo que no lo pidió: 0.0012 son 12 pips en EURUSD y una
    stop de risa en oro, y ese error tiene que ser visible, no silencioso.
    """
    c = dict(cfg or {})
    by_symbol = c.get("sl_distance_by_symbol") or {}
    if isinstance(by_symbol, dict) and symbol:
        dist = _positive(by_symbol.get(str(symbol).strip().upper()))
        if dist is not None:
            return dist, "symbol"
    dist = _positive(c.get("sl_distance"))
    if dist is not None:
        return dist, "global"
    legacy = _positive(c.get(LEGACY_SL_PIPS_KEY))
    pip = getattr(spec, "pip", None) if spec is not None else None
    if legacy is not None and pip:
        try:
            return legacy * float(pip), "legacy_pips"
        except (TypeError, ValueError):
            return None, "none"
    return None, "none"


def _positive(value: Any) -> Optional[float]:
    """El valor como float si es un número > 0; `None` si no o si es <= 0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


__all__ = [
    "DEFAULT_MIN_SCORE",
    "DEFAULTS",
    "FLAT_TO_YAML",
    "LEGACY_SL_PIPS_KEY",
    "StrategyConfigError",
    "apply_updates",
    "build_flat",
    "dump_yaml",
    "is_toplevel",
    "killzones_for",
    "load_doc",
    "min_score_for",
    "patch_text",
    "risk_weights_for",
    "sl_distance_for",
    "yaml_plain_key",
    "yaml_scalar",
]