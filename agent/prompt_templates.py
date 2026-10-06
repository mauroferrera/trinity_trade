"""Plantillas de prompt: qué se le cuenta al modelo, y en qué unidad.

Qué es "una plantilla" aquí
--------------------------
No es un `f-string` con huecos: es la decisión de **qué contexto** recibe el
decisor y **en qué magnitud**. El motor `core/risk_engine.py` calcula un score
sobre precios en unidades del bróker; el modelo lee esos mismos números en otro
sistema y los compara con lo que dice el usuario. El punto de fricción está
justo ahí: "SL de 12" son 12 pips en EURUSD, 12 PUNTOS en oro (1,20 en precio) y
12 contratos en un futuro de B3.

REF tenía ese problema resuelto a medias y enterrado en el sitio equivocado:

- El bloque de política de riesgo (`agent.py:958` `_risk_policy_lines`) estaba
  mezclado con la I/O, dentro de cuatro `try/except Exception: pass` seguidos. Un
  `except` que se traga un `KeyError` de configuración deja la política a medias sin
  que nadie lo note: el modelo recibe una política incompleta y responde como si
  fuera la completa.
- Las unidades eran las de EURUSD. El agente solo conocía los 5 decimales del forex,
  porque `app.py` solo era forex.
- El "peso de la killzone" se escribía a mano como un porcentaje que ya no era el
  del score. El propio REF lo avisa en un comentario (`agent.py:996`), pero el número
  seguía escrito en el prompt.

Aquí el prompt se **compone** con funciones puras a partir de datos: el mismo
contexto, con el peso leído de `risk_weights` y las unidades del símbolo de verdad.

El orden de las piezas
----------------------
    system  = prompt del rol            (lo que escribió el usuario, no se toca)
           + bloque de mercado          (unidades, tamaño, sesiones, macro)
           + política de riesgo         (lo que BLOQUEA y lo que solo AVISA)
           + política de fuentes        (qué dato falta y por qué)
           + instrucción dura           (el score no lo calcula el modelo)

Nada de esto decide una orden. La regla 19 de `AGENT_GUIDELINES.md` es la razón de
que la última pieza exista y sea la más explícita: un LLM que ve un score y una
política de riesgo redactará una entrada con SL y TP aunque nadie se lo pida, y esa
entrada puede acabar en un mensaje de Telegram como si fuera una recomendación
ejecutable. El prompt dice de dónde sale cada número; no dice qué hacer.

Pureza
------
Este módulo no hace I/O salvo `load_asset_map()`, que lee un YAML local de
configuración. No hay red, ni base de datos, ni `litellm`, ni `adapters`, ni `api`.
La lógica de mercado (qué es un pip, cómo se expresa el tamaño) entra como tabla y
sale como texto: por eso se puede testear una frase.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from core import clock
from core import risk_engine

#: Todo lo que el agente responde va en español. REF lo concatenaba al final del
#: mensaje de usuario (`agent.py:1112`), que es donde el modelo lo cumple peor: una
#: instrucción en el mensaje de usuario compite con lo que el usuario acaba de
#: escribir, y el usuario escribe en el idioma que escribe.
IDIOMA = "Responde siempre en español."

#: La instrucción que sostiene la separación entre explicar y decidir. Se repite
#: en el system y se repite en el bloque de mercado porque es la que más se olvida
#: cuando el usuario pregunta "y si entro aquí?".
REGLAS_DURA = (
    "El Setup Score, el SL, el TP y el veredicto los calcula core/risk_engine.py con "
    "lógica determinista: NO los recalcules ni los inventes. Usa la herramienta "
    "'setup_score' si necesitas el número y cita el valor que devuelve, con su "
    "veredicto. Si una fuente no está disponible, dilo con el motivo; una fuente "
    "ausente es información, no un cero."
)


# ---------------------------------------------------------------------------
# Perfiles de mercado
# ---------------------------------------------------------------------------
#
# Claves = `market_type` de `config/asset_sources_map.yaml`. Los tipos que no están
# en el YAML (metales e índices CFD) son los que REF negociaba y el YAML todavía no
# declara; viven aquí porque el mapa no los conoce, y hay un test que avisa cuando
# el YAML declara un `market_type` que no tiene perfil.

DESCONOCIDO = "DESCONOCIDO"

MARKET_PROFILES: Dict[str, Dict[str, Any]] = {
    "FOREX_SPOT": {
        "label": "Forex spot",
        "precio": "5 decimales en los pares de EUR/USD y GBP/USD; 3 en los pares con JPY.",
        "unidad_stop": (
            "1 pip = 0.0001 en los pares de 5 decimales y 0.01 en los de 3. Un SL de "
            "12 en EURUSD son 12 pips = 0.0012; ese mismo número en XAUUSD son 12 "
            "PUNTOS = 1,20 en precio, que no es lo mismo. Pregunta siempre en pips y "
            "confirma en precio."
        ),
        "tamano": (
            "El tamaño va en LOTES estándar (1 lote = 100.000 de la base). El "
            "calculador es 'standard_forex_lots' y el riesgo lo decide "
            "core/lot_calculator.py, no tú."
        ),
        "sesion": "Sin pausa: 24 h de lunes a viernes. Las killzones del score son UTC.",
        "macro": (
            "COT (CFTC) y correlación con el DXY. OJO: el COT solo tiene sentido para "
            "EUR/USD y su peso actual en score.weights.cot es 0, así que el COT hoy "
            "NO mueve el score; no lo vendas como una señal activa. El DXY pesa para "
            "todos los pares porque el peso es global."
        ),
        "aviso": (
            "El precio que ves es del bróker, no de la CFTC ni del DXY: son mercados "
            "distintos con horas distintas. No los compares por posición en la serie."
        ),
    },
    "INDEX_CFD": {
        "label": "Índice en CFD",
        "precio": "Puntos, no pips. Un punto de DAX son 0,1 de precio.",
        "unidad_stop": (
            "El SL se expresa en PUNTOS de índice (15 en DAX son 1,5 en precio). "
            "Confundir puntos con el precio final es el error más común aquí."
        ),
        "tamano": "Lotes de contrato por índice (1 lote = 1 contrato sobre el índice).",
        "sesion": "El CFD cotiza cuando cotiza el índice subyacente, más el horario del bróker.",
        "macro": (
            "Sin COT utilizable: el COT de la CFTC cubre futuros, no un CFD de índice. "
            "El DXY sí correlates con el riesgo de mercado, y ahí es lo útil."
        ),
        "aviso": (
            "El CFD de tu bróker y el futuro del índice real pueden tener precios "
            "distintos. Los patrones que ves son del CFD: sirven para decidir sobre el "
            "CFD y no para arbitrar contra el futuro."
        ),
    },
    "B3_FUTURES": {
        "label": "Futuros B3",
        "precio": (
            "PONTOS. WDO cotiza en dólares por punto (1 punto = 1 USD) y WIN en reais "
            "por punto. DI1 cotiza en puntos porcentuales de taxa (1 ponto = 0,01%)."
        ),
        "unidad_stop": (
            "El SL se cuenta en PONTOS y en reales/dólares por contrato, nunca en "
            "porcentaje del precio. Un 'SL de 50 puntos' en WDO son 50 USD por "
            "contrato; en WIN son 50 reais."
        ),
        "tamano": (
            "El tamaño va en contratos. B3_NT/B3_FUT usan minicontratos: el "
            "calculador es 'b3_mini_contracts' y su mínimo es 1 contrato. El "
            "redondeo es SIEMPRE a la baja."
        ),
        "sesion": (
            "Sesión diurna de la B3 con un corte a media sesión. La liquidez cambia "
            "dentro del día: config/trading_hours.json (session_id) tiene la ventana "
            "que usa el resto del sistema. Fuera de la sesión el precio existe pero "
            "no hay contraparte."
        ),
        "macro": (
            "Copom/Focus del BCB (IPCA, Selic), flujo de capital extranjero y curva DI1. "
            "Las tres están declaradas en el mapa de activos pero SIN implementar "
            "(macro_ingestor.PENDIENTES): dilo como ausencia, no como 'sin noticias'."
        ),
        "aviso": (
            "El contrato que vence se llama distinto al que cotiza (WDO != WINQ26). "
            "No te fíes de un símbolo sin sufijo de vencimiento para afirmar de qué "
            "contrato se trata."
        ),
    },
    "B3_TESOURO": {
        "label": "Tesouro B3 (renta fija)",
        "precio": (
            "Porcentaje del valor nominal, con decimales (NTN-F 13,45%). Un punto de "
            "precio NO son 1,00 de rentabilidad: 0,01% de nominal."
        ),
        "unidad_stop": (
            "El SL va en PUNTOS de tasa (0,05 = 5 centésimas de punto porcentual). "
            "El riesgo de un contrato de renta fija se mueve al revés que el de un "
            "índice cuando el tipo sube."
        ),
        "tamano": (
            "Contratos enteros, sin fracciones: el calculador es 'b3_bonds' y "
            "rechaza un tamaño fraccionario en vez de redondearlo."
        ),
        "sesion": (
            "Sesión específica de renta fija, distinta de la de futuros. La ventana "
            "está en trading_hours.json como 'b3_fixed_income' y su horario está "
            "marcado como no verificado contra el calendario oficial de la B3: no "
            "afirmes que una operación es legal fuera de ella sin comprobarlo."
        ),
        "macro": "Copom, Focus del BCB y curva DI1. Sin implementar (ver B3_FUTURES).",
        "aviso": "La curva DI1 es la referencia natural del precio, pero no está conectada.",
    },
    "CRYPTO_PERPETUAL": {
        "label": "Cripto perpetuo",
        "precio": (
            "Precio en USDT. No hay pip: el salto mínimo es un tick y su tamaño "
            "depende del símbolo (0,01 / 0,1 / 1 según el par)."
        ),
        "unidad_stop": (
            "El SL se expresa en precio y en PORCENTAJE respecto a la entrada. Da "
            "siempre las dos cifras: un 2% sobre 60.000 son 1.200, y sin el precio el "
            "porcentaje no significa nada."
        ),
        "tamano": (
            "El tamaño va en unidades de la BASE (BTC, ETH) o en nocional USDT. El "
            "nocional necesita el precio de entrada: sin él no hay forma de pasar de "
            "unidades a USDT, y core/lot_calculator.py lo dice en vez de inventar un "
            "notional."
        ),
        "sesion": "24x7, sin cierre. El 'día de trading' de los contadores es artificial: se define en la config.",
        "macro": (
            "Funding rate, mapa de liquidaciones, Fear & Greed y flujo on-chain: "
            "declarados en el mapa y SIN implementar. El funding es el equivalente "
            "funcional del COT (quién está pagando por el sesgo), pero hoy no hay "
            "dato: no lo sustituyas por intuición."
        ),
        "aviso": (
            "Un perpetuo puede liquidar por mantenimiento antes de que se vea el "
            "stop. La distancia al SL no es la distancia al riesgo real, y la "
            "calcula el motor, no tú."
        ),
    },
    DESCONOCIDO: {
        "label": "Símbolo sin ficha",
        "precio": "No lo sé: el símbolo no está en config/asset_sources_map.yaml.",
        "unidad_stop": (
            "NO inventes unidades. Di que el símbolo no está en el mapa de activos y "
            "que no puedes traducir 'pips' o 'puntos' sin saber su convención. Un SL "
            "expresado en la unidad equivocada es un error de un 100%."
        ),
        "tamano": "No lo sé: el tamaño depende del calculador del mercado y no lo puedo derivar.",
        "sesion": "No lo sé: la sesión depende del mercado.",
        "macro": "No lo sé: no hay fuentes declaradas para este símbolo.",
        "aviso": (
            "Puedo leer precio y patrones del bróker para este símbolo, pero su "
            "convención de unidades es desconocida. Da los números en PRECIO y "
            "declara que la conversión no está verificada."
        ),
    },
}

#: Símbolos que REF negociaba y que `asset_sources_map.yaml` todavía no declara.
#: Están aquí para que el agente no degrade a "símbolo sin ficha" lo que sí sabe
#: negociar, y a la vez quede escrito que la tabla es del agente, no del mapa.
SIMBOLOS_FUERA_DEL_YAML: Dict[str, str] = {
    # Metales en MT5: spot con 2 decimales, pip = 0,01.
    "XAUUSD": "FOREX_SPOT",
    "XAGUSD": "FOREX_SPOT",
    # Índices en CFD sobre MT5.
    "NAS100": "INDEX_CFD",
    "USTEC": "INDEX_CFD",
    "US500": "INDEX_CFD",
    "US30": "INDEX_CFD",
    "GER30": "INDEX_CFD",
    "DAX": "INDEX_CFD",
}

#: Sufijos que identifican un mercado sin mirar el YAML. Deliberadamente corto: la
#: regla solo cubre el caso obvio (un par contra un establecoin) y el resto cae en
#: "sin ficha", que es una respuesta honesta. Adivinar más sería inventar la unidad
#: de un símbolo, que es el error que más caro sale.
SUFIJOS_CLAVE = ("USDT", "USDC", "BUSD", "FDUSD", "PERP")


# ---------------------------------------------------------------------------
# El mapa de activos
# ---------------------------------------------------------------------------


def load_asset_map(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Lee `config/asset_sources_map.yaml` a `{SIMBOLO: entrada}`.

    Es la ÚNICA función de este módulo que toca el disco, y lee configuración local
    (no red, no base de datos). Existe aquí y no en un `config_loader.py` porque
    solo hay un consumidor y un módulo de una función es ruido.

    Devuelve `{}` si el fichero no existe o no es un mapeo. Un YAML roto en un
    archivo de CONFIGURACIÓN no puede tumbar el chat: el agente degrada a "sin
    ficha" por símbolo, que es el mismo comportamiento que si el símbolo no
    estuviera en el mapa.
    """
    from core import paths

    ruta = path or os.environ.get("ASSET_SOURCES_PATH") or os.path.join(
        paths.CONFIG_DIR, "asset_sources_map.yaml"
    )
    try:
        import yaml

        with open(ruta, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001 - configuración ausente, no excepción de runtime
        return {}
    if not isinstance(doc, dict):
        return {}
    return {str(k).strip().upper(): v for k, v in doc.items() if isinstance(v, dict)}


def market_for(symbol: str, asset_map: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """`market_type` de un símbolo. Puro: el mapa entra como argumento.

    Tres pasos y ninguno adivina:

    1. El mapa de activos (fuente de verdad) si declara ese símbolo.
    2. La tabla de símbolos que el mapa no declara todavía.
    3. Un sufijo de establecoin, que es el único caso que se resuelve por forma.

    Lo que no hay es un `else` que diga "forex". Devolver el perfil equivocado hace
    que el modelo traduzca 'pips' a 'puntos' en un activo donde no son lo mismo, y
    eso es un error de sizing, no de redacción.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return DESCONOCIDO
    if asset_map:
        entrada = asset_map.get(sym)
        if isinstance(entrada, dict):
            mt = str(entrada.get("market_type") or "").strip().upper()
            if mt:
                return mt
    if sym in SIMBOLOS_FUERA_DEL_YAML:
        return SIMBOLOS_FUERA_DEL_YAML[sym]
    if sym.endswith(SUFIJOS_CLAVE):
        return "CRYPTO_PERPETUAL"
    return DESCONOCIDO


def profile_for(market_type: str) -> Dict[str, Any]:
    """Perfil de un `market_type`, con el de "sin ficha" como red de seguridad."""
    return MARKET_PROFILES.get(str(market_type or "").strip().upper(), MARKET_PROFILES[DESCONOCIDO])


# ---------------------------------------------------------------------------
# Temas (qué herramienta merece la pena ante una pregunta)
# ---------------------------------------------------------------------------
#
# REF guardaba estos precios en `agent_topics` del YAML y los comparaba con `startswith`
# de token. Se conservan tal cual (`startswith` sobre el token, no `in`), porque es lo
# que hace que "comprar" active el tema de trading sin que "compre" lo haga: el LLM
# escribe con la raíz del verbo.
#
# Lo nuevo es el juego por mercado: las palabras de B3 y de cripto no aparecen en la
# lista de forex de REF, así que preguntar "¿cómo va el cupom?" no activaba nada.

DEFAULT_TOPICS: Dict[str, List[str]] = {
    "account": ["saldo", "equity", "margen", "balance", "capital", "cuenta", "account"],
    "positions": ["posicion", "abierta", "abierto", "trades"],
    "history": ["historial", "historia", "cerrada", "historico", "gan"],
    "news": ["noticia", "noticias", "economi", "calendario", "dato", "pip", "fed", "nipc", "inflacion"],
    "trade": [
        "oper", "entrar", "entrada", "setup", "compr", "vend", "buy", "sell",
        "stoploss", "sl", "tp", "take", "riesgo", "score", "ganar", "perder",
    ],
}

MARKET_TOPICS: Dict[str, Dict[str, List[str]]] = {
    "FOREX_SPOT": {
        "trade": ["pip", "pips", "lote", "lot", "dolar", "euro", "chf", "yen", "nivel"],
    },
    "INDEX_CFD": {
        "trade": ["punto", "indice", "dax", "nasdaq", "sp500", "dow", "puntos"],
        "news": ["copom", "inflation", "empleo", "nfp", "pmi"],
    },
    "B3_FUTURES": {
        "trade": ["contrato", "contratos", "mini", "ponto", "pontos", "lote", "wdo", "win", "dolar", "real"],
        "news": ["copom", "focus", "ipca", "selic", "pib", "bcb", "juros"],
    },
    "B3_TESOURO": {
        "trade": ["tesouro", "ntn", "ltn", "lft", "di", "juros", "cupom", "titulo", "duration"],
        "news": ["copom", "focus", "selic", "juros", "ipca"],
    },
    "CRYPTO_PERPETUAL": {
        "trade": ["long", "short", "alavancagem", "perpetuo", "btc", "eth", "sol", "usdt", "liquida", "funding"],
        "news": ["etf", "halving", "regulacao", "sec", "hack", "fear", "greed"],
    },
}

COMPLEX_HINTS: Sequence[str] = (
    "analiza", "análisis", "analisis", "analizar", "order flow", "patrones", "patrón",
    "smc", "gráfico", "grafico", "graficar", "evaluar", "recomendación", "recomendacion",
    "setup", "tendencia", "cvd", "delta", "volumen", "absorb", "absorción", "barrido",
    "pdh", "pdl", "fvg", "order block", "liquidez", "liquidity", "chart", "economic_news",
    "noticias", "escenario", "entrada", "operar", "signals", "señal",
    "alarma", "alerta", "alert", "avisa", "avisame", "notific",
)

MARKET_COMPLEX_HINTS: Dict[str, Sequence[str]] = {
    "B3_FUTURES": ("cupom", "contrato", "mini", "vencimento", "ajuste", "funding b3"),
    "B3_TESOURO": ("duration", "carteira", "mark-to-market", "preco", "juros"),
    "CRYPTO_PERPETUAL": ("funding", "liquidacion", "liquidación", "perpetuo", "long", "short"),
}


def topics_for(market_type: str, base: Optional[Dict[str, List[str]]] = None) -> Dict[str, List[str]]:
    """Topics base + los del mercado. Las palabras del base NO se pisan."""
    salida: Dict[str, List[str]] = {
        k: list(v) for k, v in (base if base is not None else DEFAULT_TOPICS).items()
    }
    for tema, palabras in MARKET_TOPICS.get(str(market_type or "").upper(), {}).items():
        merged = list(salida.get(tema, []))
        for palabra in palabras:
            if palabra not in merged:
                merged.append(palabra)
        salida[tema] = merged
    return salida


def _tokens(mensaje: str) -> List[str]:
    return [w.strip(".,;:!?()\"'«»") for w in str(mensaje or "").lower().split()]


def menciona(mensaje: str, palabras: Iterable[str]) -> bool:
    """¿El mensaje toca alguna de esas palabras?

    `startswith` sobre el token, no `in` sobre la frase: REF comparaba así y con
    `in` la palabra "sl" activaba el tema de trading dentro de "isla" o "atraco".
    La raíz del token es lo que el LLM escribe.

    El coste del prefijo es explícito, no un descuido: "sla" y "slide" empiezan por
    "sl" y disparan el tema. Se acepta porque el falso positivo abre herramientas de
    consulta (el modelo ve los datos y decide), mientras que el falso negativo lo
    hace redactar de memoria. Fallar hacia el lado que consulta es más barato que
    fallar hacia el lado que inventa.
    """
    tokens = _tokens(mensaje)
    for palabra in palabras or ():
        raiz = str(palabra or "").strip().lower()
        if raiz and any(t.startswith(raiz) for t in tokens):
            return True
    return False


def is_complex_request(mensaje: str, market_type: str = DESCONOCIDO) -> bool:
    """¿La petición necesita herramientas, o basta con redactar?

    REF decidía esto con una lista de 40 palabras que era toda de forex (patrones,
    PDH/PDL, order flow). Un "análisis del cupom" no la activaba y el modelo
    respondía de memoria, sin consultar nada: exactamente el fallo que la lista
    intentaba evitar. Ahora la lista tiene una parte por mercado.
    """
    texto = str(mensaje or "").lower()
    hints = list(COMPLEX_HINTS) + list(
        MARKET_COMPLEX_HINTS.get(str(market_type or "").upper(), ())
    )
    return any(h in texto for h in hints)


# ---------------------------------------------------------------------------
# Bloques de prompt
# ---------------------------------------------------------------------------


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, bool) or value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_loads(value: Any, default: Any) -> Any:
    """El config de trading lleva el peso y las ventanas como TEXTO JSON.

    Es el shape histórico que esperan la API y los EAs (D-006: `strategy.yaml` manda,
    el dict plano es lo que circula). Parsearlo aquí, con default, es lo que
    permite que un `risk_weights` corrupto no se coma el prompt entero.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _cfg_val(cfg: Optional[Dict[str, Any]], plano: str, *anidado: str) -> Any:
    """Lee una clave del YAML tanto anidada como plana.

    `config/strategy.yaml` es anidado (`score.weights`, `prop.enabled`,
    `prop.max_dd_daily_pct`), pero D-006 fijó que por la API y los EAs circule el
    dict plano (`risk_weights`, `prop_enabled`, `prop_max_dd_daily_pct`). Esta
    función acepta las dos formas.

    Lo que no acepta es la ausencia: si ninguna de las dos está, devuelve `None` y
    quien llama lo DICE. Antes esta función no existía y el prompt leía unas claves
    planas que el YAML no tiene: con `prop.enabled: true` en el YAML, el prompt
    anunciaba "prop firm: desactivado" porque `prop_enabled` no estaba, y el peso de
    la killzone salía de los defaults de `core`, presentado como el de la config.
    """
    cfg = cfg or {}
    valor = cfg.get(plano)
    if valor is not None:
        return valor
    actual: Any = cfg
    for paso in anidado:
        if not isinstance(actual, dict):
            return None
        actual = actual.get(paso)
        if actual is None:
            return None
    return actual


def market_block(market_type: str, entrada: Optional[Dict[str, Any]] = None) -> str:
    """El bloque de convenciones del mercado, para el system prompt."""
    perfil = profile_for(market_type)
    lineas = [
        "## Mercado: {0}".format(perfil["label"]),
        "- Precio y unidad: {0}".format(perfil["precio"]),
        "- Unidad del stop loss: {0}".format(perfil["unidad_stop"]),
        "- Tamaño de la posición: {0}".format(perfil["tamano"]),
        "- Sesión y liquidez: {0}".format(perfil["sesion"]),
        "- Fuentes macro declaradas: {0}".format(perfil["macro"]),
    ]
    if perfil.get("aviso"):
        lineas.append("- Aviso: {0}".format(perfil["aviso"]))
    if entrada:
        lineas.append(
            "- Activo en el mapa: session_id={0} · data_adapter={1} · lot_calculator={2}.".format(
                entrada.get("session_id", "?"),
                entrada.get("data_adapter", "?"),
                entrada.get("lot_calculator", "?"),
            )
        )
    return "\n".join(lineas)


def risk_policy_lines(
    cfg: Optional[Dict[str, Any]],
    estado: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> List[str]:
    """La política de riesgo en texto, una línea por regla.

    Es la versión sin I/O de `REF/agent.py:958`. Tres diferencias que no son de
    estilo:

    1. **El peso de la killzone se CALCULA** (`component_share`) en vez de estar
       escrito. REF ya avisaba en un comentario de que el 20% era mentira; aquí el
       número sale de `risk_weights`, y si mañana el killzone pesa 0, la línea dice 0%.
    2. **Nada se traga en silencio.** REF tenía cuatro `except: pass`: si
       `risk_weights` venía corrupto, la línea de la killzone desaparecía y el modelo
       recibía una política sin ella, indistinguible de una política que no tiene
       killzone. Aquí, o se calcula o se dice que no se pudo calcular.
    3. **La ausencia de datos es visible.** Si no hay estado diario (MT5 caído), la
       línea lo dice; no se omite para que el prompt quede limpio.
    """
    cfg = cfg or {}
    estado = estado or {}
    out: List[str] = []

    if estado.get("error"):
        out.append(
            "- Política de riesgo: SIN ESTADO EN VIVO ({0}). No puedes afirmar DD ni "
            "operaciones de hoy.".format(estado.get("error"))
        )
    elif estado:
        if estado.get("blocked"):
            motivos = "; ".join(str(m) for m in (estado.get("reasons") or []) if m)
            out.append(
                "- Política de riesgo: OPERAR BLOQUEADO (drawdown diario)"
                + (": {0}".format(motivos) if motivos else "")
                + ". No propongas entrada mientras siga así."
            )
        else:
            out.append(
                "- Política de riesgo diaria (día de trading {0}): DD {1:,.2f} ({2:.2f}%) · "
                "{3}/{4} operaciones · tope {5:,.0f} / {6}%.".format(
                    estado.get("trading_day", "?"),
                    _num(estado.get("dd_daily")),
                    _num(estado.get("dd_pct")),
                    estado.get("trades_today", 0) or 0,
                    estado.get("max_trades_day", 0) or 0,
                    _num(estado.get("max_loss_fixed")),
                    _num(estado.get("max_loss_pct")),
                )
            )

    ventanas = _json_loads(_cfg_val(cfg, "killzones", "killzones"), [])
    pesos = _json_loads(_cfg_val(cfg, "risk_weights", "score", "weights"), None)
    try:
        kz = risk_engine.killzone_score(ventanas, now=now)
        donde = "DENTRO de {0}".format(kz["name"]) if kz["in_killzone"] else "FUERA de killzone"
        if isinstance(pesos, dict) and pesos:
            try:
                cuota = risk_engine.component_share(pesos, "killzone")
                donde += " (peso {0:.0f}% del score)".format(cuota * 100)
            except Exception:  # noqa: BLE001 - el peso es informativo
                donde += " (peso no calculado: score.weights ilegible)"
        else:
            donde += " (peso no calculado: score.weights ausente o ilegible)"
        out.append("- Killzone (UTC): {0}. La killzone solo AVISA y baja el score: nunca congela.".format(donde))
    except Exception as exc:  # noqa: BLE001 - no calculable se DICE, no se omite
        out.append("- Killzone (UTC): no calculable ({0}).".format(exc))

    prop_on = bool(_cfg_val(cfg, "prop_enabled", "prop", "enabled"))
    if prop_on and estado.get("prop_enabled"):
        out.append(
            "- Modo prop firm (ENFORCED): DD diario {0}%/{1}% · DD total {2}%/{3}% · "
            "ganancia del día {4}%/{5}%.".format(
                _num(estado.get("prop_dd_daily_pct")),
                _num(_cfg_val(cfg, "prop_max_dd_daily_pct", "prop", "max_dd_daily_pct")),
                _num(estado.get("prop_dd_total_pct")),
                _num(_cfg_val(cfg, "prop_max_dd_total_pct", "prop", "max_dd_total_pct")),
                _num(estado.get("prop_day_profit_pct")),
                _num(_cfg_val(cfg, "prop_max_profit_day_pct", "prop", "max_profit_day_pct")),
            )
        )
    elif prop_on:
        out.append(
            "- Modo prop firm (ENFORCED): límites {0}% DD diario / {1}% DD total / tope de "
            "ganancia {2}% (métricas en vivo no disponibles).".format(
                _num(_cfg_val(cfg, "prop_max_dd_daily_pct", "prop", "max_dd_daily_pct")),
                _num(_cfg_val(cfg, "prop_max_dd_total_pct", "prop", "max_dd_total_pct")),
                _num(_cfg_val(cfg, "prop_max_profit_day_pct", "prop", "max_profit_day_pct")),
            )
        )
    else:
        out.append("- Modo prop firm: desactivado en configuración.")

    if prop_on:
        out.append(
            "- Sobre el prop firm: `consistency_days` está en la configuración pero NINGÚN "
            "código lo mira. No prometas ni anuncies 'consistencia de N días'."
        )

    fuentes = cfg.get("data_sources") or {}
    noticias_bloquean = bool(fuentes.get("news", True))
    out.append(
        "- Enforcement: el drawdown diario y el modo prop BLOQUEAN duro. Las noticias "
        "{0} ±{1} min (impacto alto; el calendario es fail-open, así que si no lo "
        "pudiste leer DILO). La killzone solo avisa y baja el score.".format(
            "BLOQUEAN duro" if noticias_bloquean else "NO bloquean",
            int(_num(cfg.get("news_buffer_min"), 15)),
        )
    )
    return out


def data_sources_policy(cfg: Optional[Dict[str, Any]], lecturas: Optional[Dict[str, Any]] = None) -> List[str]:
    """Qué fuente está activa, cuál falta y por qué.

    REF anunciaba en el prompt que había "noticias de Forex Factory" sin decir si la
    fuente estaba viva. Después de D-022/D-024 cada lectura lleva `stale` y `reason`,
    así que aquí se puede decir la verdad: si la fuente no se pudo leer, el modelo
    recibe la ausencia con su motivo en vez de un silencio que parece un "todo bien".
    """
    cfg = cfg or {}
    fuentes = cfg.get("data_sources") or {}
    out = [
        "- Score: {0}".format(
            ", ".join(
                "{0} {1}{2}".format(
                    nombre, "ON" if activo else "OFF", " (stale)" if ((lecturas or {}).get(nombre) or {}).get("stale") else ""
                )
                for nombre, activo in sorted(fuentes.items())
            )
            or "sin fuentes declaradas"
        )
    ]
    degradadas = []
    for nombre, lectura in sorted((lecturas or {}).items()):
        motivo = (lectura or {}).get("reason")
        if motivo:
            degradadas.append("{0} ({1})".format(nombre, motivo))
    if degradadas:
        out.append("- Fuentes degradadas en esta consulta: {0}.".format("; ".join(degradadas)))
    return out


def live_data_block(
    lineas_datos: Sequence[str],
    politica: Sequence[str] = (),
    policy_header: str = "Datos consultados en vivo (usa SOLO estos valores, NUNCA inventes cifras):",
) -> str:
    """El bloque de datos en vivo que se inyecta como mensaje de sistema.

    Es un bloque y no una lista de mensajes sueltos porque el modelo lo lee como un
    documento: separado por viñetas y con la política debajo, la primera lectura es
    "esto es lo que hay", no "esto es lo que/opino".
    """
    partes = []
    if lineas_datos:
        partes.append("\n".join(lineas_datos))
    if politica:
        partes.append("\n".join(politica))
    if not partes:
        return ""
    return policy_header + "\n" + "\n\n".join(partes)


def clock_lines(
    hora_utc: Optional[str] = None,
    hora_broker: Optional[str] = None,
    dia_trading: Optional[str] = None,
) -> List[str]:
    """Las tres horas que el modelo necesita, cada una por su motivo.

    - **UTC**: es lo que ven las velas, las killzones y los timestamps del gráfico.
      REF ya lo arreglaba, y sigue siendo la hora que hay que dar primero.
    - **Broker**: para contrastar con lo que dice el terminal.
    - **Día de trading**: los contadores diarios (`max_trades_day`, DD diario, HWM)
      cuentan por día de BROKER, no por día UTC. Dar solo la hora UTC hace que el
      modelo diga que "hoy" es un día en el que el tope ya se reseteó cuando no es así.
    """
    out = [
        "- Hora actual (UTC): {0}".format(hora_utc or clock.fmt_utc()),
    ]
    if hora_broker:
        out.append("- Hora del servidor del broker: {0}".format(hora_broker))
    if dia_trading:
        out.append("- Día de trading (contadores diarios, DD y HWM): {0}".format(dia_trading))
    return out


def system_prompt(
    prompt_rol: Optional[str],
    market_type: str = DESCONOCIDO,
    cfg: Optional[Dict[str, Any]] = None,
    entrada: Optional[Dict[str, Any]] = None,
    lecturas: Optional[Dict[str, Any]] = None,
) -> str:
    """El system prompt completo: rol + mercado + política + la regla dura.

    El prompt del rol va PRIMERO y no se toca. Es lo que el usuario ha escrito para
    este agente, y prependerle contexto del sistema lo convierte en una instrucción
    más entre otras: el rol es el que decide el carácter de la respuesta.

    La regla dura va al FINAL. Es la instrucción con más probabilidades de ser
    incumplida, porque es la que compite con el impulso del modelo a completar el
    patrón "score + política → entrada con SL y TP". Lo que se lee último es lo que
    sobrevive al contexto largo.
    """
    partes = []
    if prompt_rol:
        partes.append(str(prompt_rol).strip())
    partes.append(market_block(market_type, entrada))
    partes.append("## Política de riesgo\n" + "\n".join(risk_policy_lines(cfg, None)))
    partes.append("## Fuentes\n" + "\n".join(data_sources_policy(cfg, lecturas)))
    partes.append("## Reglas\n- " + REGLAS_DURA)
    return "\n\n".join(p for p in partes if p)


# ---------------------------------------------------------------------------
# Traza de herramientas (lo que se persiste)
# ---------------------------------------------------------------------------
#
# REF guardaba el JSON entero del resultado de cada herramienta en la fila
# `role='tool'`. Dos consecuencias que solo se ven con el tiempo:
#
#   - `patterns` y `chart_snapshot` devuelven velas y patrones: cientos de KB por
#     llamada, y la tabla `messages` es la que se lee al abrir una conversación.
#     Después de un rato de análisis la traza pesa más que todo el historial útil.
#   - La fila decía QUÉ respondió la herramienta, nunca qué se le pidió. Una traza
#     sin argumentos no permite reproducir la consulta ni auditar qué se decidió con ella.
#
# El sobre de aquí lleva los argumentos, el estado, el tamaño real y un recorte
# declarado. Lo que se entrega al MODELO en la ronda sigue siendo el payload
# completo: la recorte es de la traza, no de la respuesta.

#: Presupuesto del `data` persistido, en caracteres. Justo para que quepa un
#: resumen legible de una consulta y no una lista de velas.
MAX_DIGEST_CHARS = 1200


def _recorta(valor: Any, presupuesto: int) -> Any:
    """Deja el valor entero si cabe; si no, un marcador que lo DICE."""
    try:
        texto = json.dumps(valor, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - algo no serializable no puede romper la traza
        texto = str(valor)
    if len(texto) <= presupuesto:
        return valor
    return {
        "truncado": True,
        "chars": len(texto),
        "preview": texto[:presupuesto] + "…",
    }


def tool_digest(
    name: str,
    args: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
    round_no: Optional[int] = None,
    elapsed_ms: Optional[float] = None,
    now: Optional[datetime] = None,
    presupuesto: int = MAX_DIGEST_CHARS,
) -> str:
    """El sobre JSON que se persiste en `messages(role='tool')`.

    Campos y por qué están todos:

    - `tool` / `args`: qué se pidió. Sin esto la traza no es reproducible.
    - `round`: en qué ronda del bucle se pidió. Con los args basta para reconstruir.
    - `status`: el vocabulario de `agent.ports` (ok/failed/unavailable). La
      distinción entre "el bróker no respondió" y "este puerto no existe" no se
      puede recuperar del texto de error después.
    - `bytes`: el tamaño REAL de la respuesta, aunque se recorte. Sin esto, un
      recorte es indistinguible de una respuesta corta.
    - `truncado` (dentro de `data`): la traza declara que está recortada.
    - `at`: sello UTC, el mismo formato que `core.clock.now_iso()`.

    Se serializa a texto porque la columna es TEXT y porque el agente del chat lo
    relee tal cual: un sobre es auditable con un `SELECT` y sin este repo.
    """
    result = result or {}
    data = result.get("data")
    recortado = _recorta(data, presupuesto)
    sobre = {
        "tool": str(name or ""),
        "args": _recorta(dict(args or {}), 400),
        "round": round_no,
        "status": result.get("status"),
        "at": clock.now_iso(now),
        "elapsed_ms": round(elapsed_ms, 1) if isinstance(elapsed_ms, (int, float)) else None,
        "bytes": len(json.dumps(data, ensure_ascii=False, default=str)) if data is not None else 0,
        "data": recortado,
    }
    if result.get("error") is not None:
        sobre["error"] = str(result.get("error"))
    if isinstance(recortado, dict) and recortado.get("truncado"):
        sobre["truncado"] = True
    return json.dumps(sobre, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Dibujos del usuario
# ---------------------------------------------------------------------------

TOOL_NOMBRES = {
    "line": "línea de tendencia",
    "hline": "nivel horizontal",
    "rect": "rectángulo",
    "measure": "medición",
    "text": "texto",
}


def summarize_drawings(
    drawings: Optional[Sequence[Dict[str, Any]]],
    symbol: str,
    timeframe: str,
    limite: int = 50,
) -> Dict[str, Any]:
    """Los dibujos del usuario en texto legible, más el volcado crudo.

    Función pura (`REF/agent.py:682` `_summarize_drawings` leía de `store`): el LLM
    lee la línea "3. [usuario] nivel horizontal: nivel 1.16500" mucho mejor que un
    objeto, pero el volcado crudo se conserva porque las coordenadas exactas son las
    que necesita quien va a dibujar y las que no se deben volver a pedir al usuario.
    """
    lista = [d for d in (drawings or []) if isinstance(d, dict)]
    lineas = []
    for i, d in enumerate(lista[:limite], 1):
        tool = TOOL_NOMBRES.get(d.get("tool"), str(d.get("tool") or "dibujo"))
        origen = "agente" if d.get("origin") == "agent" else "usuario"
        tk = d.get("tool")
        try:
            if tk == "hline":
                detalle = "nivel {0:.5f}".format(float(d.get("p0")))
            elif tk == "text":
                detalle = "'{0}' en t={1} p={2:.5f}".format(
                    d.get("text", ""), d.get("t0"), _num(d.get("p0"))
                )
            elif tk in ("line", "rect", "measure"):
                detalle = "de (t={0}, p={1:.5f}) a (t={2}, p={3:.5f})".format(
                    d.get("t0"), _num(d.get("p0")), d.get("t1"), _num(d.get("p1"))
                )
            else:
                detalle = json.dumps(d, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            detalle = json.dumps(d, ensure_ascii=False, default=str)
        lineas.append("{0}. [{1}] {2}: {3}".format(i, origen, tool, detalle))
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(drawings or []),
        "drawings": list(drawings or []),
        "lectura": "; ".join(lineas) if lineas else "No hay dibujos guardados para este símbolo/timeframe.",
        "nota": "Los tiempos están en epoch segundos; coordina con 'patterns'/'chart_snapshot' del mismo símbolo y timeframe.",
    }


def describe_number(value: Any, decimales: int = 5) -> str:
    """Número para humanos, o `?` si no hay número.

    Existe porque en un prompt un `None` impreso es "None" y una cadena vacía es
    "", y el modelo los rellena con lo que le parece. `?` es una respuesta.
    """
    if value is None or value == "":
        return "?"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(x):
        return "?"
    return ("{0:,.%df}" % decimales).format(x)


__all__ = [
    "COMPLEX_HINTS",
    "DEFAULT_TOPICS",
    "DESCONOCIDO",
    "IDIOMA",
    "MARKET_COMPLEX_HINTS",
    "MARKET_PROFILES",
    "MARKET_TOPICS",
    "MAX_DIGEST_CHARS",
    "REGLAS_DURA",
    "SIMBOLOS_FUERA_DEL_YAML",
    "SUFIJOS_CLAVE",
    "clock_lines",
    "data_sources_policy",
    "describe_number",
    "is_complex_request",
    "live_data_block",
    "load_asset_map",
    "market_block",
    "market_for",
    "menciona",
    "profile_for",
    "risk_policy_lines",
    "summarize_drawings",
    "system_prompt",
    "tool_digest",
    "topics_for",
]
