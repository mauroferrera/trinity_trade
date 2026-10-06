"""El puente: bucle de herramientas, router de proveedores y stream SSE.

Qué es este módulo
------------------
`REF/agent.py` tenía 1.308 líneas con las tres cosas metidas en el mismo archivo:
el router de modelos, el bucle de tool-calling y el generador SSE. Aquí están
separadas en tres pisos que ya existen:

- `agent.tools` sabe ejecutar una herramienta y decir si su puerto está.
- `agent.prompt_templates` sabe construir el texto que lee el modelo.
- `agent.laya_bridge` (este) decide **a quién se le pregunta, en qué modo y qué se
  le responde**: eso no lo sabe hacer nadie más.

Lo que este módulo NO hace, y es lo importante
----------------------------------------------
No calcula scores, ni SL/TP, ni tamaños, ni decide si se opera. El score lo calcula
`core/risk_engine.py` y la política la redactan las plantillas. Aquí solo se
transportan. La razón es la regla 19 de `AGENT_GUIDELINES.md`: si el bucle puede
"ajustar" un número, ese número deja de ser auditable.

El seam de `completion`
-----------------------
LiteLLM no se importa nunca aquí. El puente llama a un `completion` inyectado, con
la misma firma que `litellm.acompletion`; el default lo resuelve en la primera
llamada. Dos razones, y las dos son de test:

1. `import agent` no puede abrir una sesión de MT5 ni cargar un cliente de LLM
   (D-017). Un import con efecto secundario en el módulo del agente fue
   exactamente el problema que motivó los puertos.
2. El bucle con fallback, rotación de claves y backoff es la parte con más caminos
   de error de todo el agente. Probarlo contra un proveedor real sería probar la
   red, no la lógica.

Las diferencias con REF que son correcciones, no estilo
------------------------------------------------------
1. **`done` es un invariante del módulo, no de cada rama.** REF lo emitía en cinco
   sitios: añadir un camino nuevo y se olvidaba, y el cliente se queda con un
   stream abierto para siempre. Aquí el generador interno NUNCA emite `done`; lo
   emite el envoltorio, una vez, sea cual sea el camino. Y si el cliente
   desconecta (GeneratorExit) no se emite, porque no hay nadie que lo lea.
2. **Un stream cortado a medias NO cambia de proveedor si ya salió texto.** REF
   emitía los `delta` en vivo y, si el stream se cortaba, probaba el siguiente
   proveedor: el usuario veía media respuesta del modelo A y luego la del B, una
   encima de otra. Aquí la vuelta atrás solo existe antes del primer `delta`;
   después se dice que se cortó y se termina. Media respuesta es mejor que dos
   respuestas superpuestas.
3. **La traza y el modelo no ven lo mismo, a propósito.** Lo que se PERSISTE es el
   sobre de `prompt_templates.tool_digest` (con argumentos, estado, tamaño y
   recorte declarado); lo que se entrega al modelo en la ronda es el payload
   completo. REF guardaba el JSON entero del resultado, que en `chart_snapshot` son
   cientos de KB por llamada, y no guardaba los argumentos: una traza sin ellos no
   permite reproducir la consulta.
4. **El reloj se inyecta también en modo herramientas.** REF solo lo daba en la ruta
   simple, y la política de riesgo que lee el modelo en modo herramientas habla de
   killzones en UTC. Sin la hora, esa política no se puede aplicar.
5. **El historial se lee ANTES de persistir el turno del usuario.** REF
   persistía el mensaje y luego lo releía con el historial, de modo que la pregunta
   del usuario llegaba al modelo dos veces: una como historial y otra como mensaje
   final. Con un turno largo el modelo ve su propia pregunta repetida.
6. **Los settings llegan como texto.** `store.get_settings()` devuelve
   `dict[str, str]`; `int("6")` funciona y `int("seis")` revienta el stream.
   `_ajusta_int` y `_ajusta_lista` hacen esa traducción con un default explícito.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from agent import prompt_templates as pt
from agent import tools as toolbox
from agent.ports import STATUS_FAILED, STATUS_OK, AgentDeps


# ---------------------------------------------------------------------------
# Router de proveedores
# ---------------------------------------------------------------------------
#
# El prefijo del modelo es el que LiteLLM entiende (`gemini/…`, `openai/…`). Los
# alias `google/` y `claude/` no son de LiteLLM: son lo que la gente escribe, y sin
# esta tabla un rol con `provider="claude"` acabaría en un modelo que no existe.

PROVIDER_ENV: Dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
}

#: Prefijos que la gente escribe y LiteLLM no entiende. Sin esta tabla, un rol con
#: `provider="claude"` acabaría pidiendo `claude/sonnet` y la respuesta sería un 404
#: del proveedor que no dice "el prefijo está mal".
PROVIDER_ALIAS: Dict[str, str] = {"google": "gemini", "claude": "anthropic"}

DEFAULT_CHAIN: Tuple[str, ...] = ("gemini/gemini-3.8-flash", "gemini/gemini-3.5-flash")

LOCAL_PREFIX = "ollama"

#: Modelos que ya no existen en LiteLLM. Sin esta tabla, un rol guardado con uno de
#: ellos falla con un 404 del proveedor que no dice "el nombre está mal".
LEGACY_MODEL_MAP: Dict[str, str] = {
    "gemini/gemini-3.6-flash": "gemini/gemini-3.8-flash",
    "gemini/gemini-3.7-flash": "gemini/gemini-3.8-flash",
    "google/gemini-3.6-flash": "gemini/gemini-3.8-flash",
    "google/gemini-3.7-flash": "gemini/gemini-3.8-flash",
}

#: Rotación de claves de Gemini. Un 429 por cuota es de la CLAVE, no del modelo:
#: sin rotar, el fallback avanza al modelo siguiente del mismo proveedor y se
#: pierde la causa real del fallo.
GEMINI_KEY_ENVS: Tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_2",
    "GEMINI_API_KEY_3",
    "GEMINI_API_KEY_4",
    "GEMINI_API_KEY_5",
)

#: Timeout por modelo. El local es un Ollama en CPU: 45 s lo cortaban a mitad de
#: una respuesta larga, y al cliente le llegaba un error sin texto.
TIMEOUT_S_LOCAL = 180.0
TIMEOUT_S_REMOTO = 45.0

#: Reintentos por límite de peticiones, sobre la MISMA clave y el MISMO modelo,
#: antes de pasar a la siguiente tarea. Es un backoff corto porque el 429 de estos
#: proveedores dura segundos: rotar antes de esperar quema las claves de golpe.
MAX_INTENTOS_LIMITE = 3
BACKOFF_BASE_S = 0.5

DEFAULT_HISTORY_LIMIT = 20
DEFAULT_MAX_TOOL_ROUNDS = 6

#: Lo que se persiste cuando el modelo cierra una ronda sin texto. Es preferible a
#: dejar el turno del usuario sin respuesta: la siguiente pregunta llega sin
#: contexto de por qué se quedó a medias.
SIN_RESPUESTA = "Sin respuesta textual del modelo."
RONDAS_EXCESO = "El agente alcanzó el máximo de rondas de herramientas."

EVENTO_ERROR = "error"
EVENTO_DONE = "done"

#: Etiqueta legible de cada herramienta de la ruta simple. El modelo recibe el JSON
#: crudo debajo de una frase que explica qué es, porque un `{"balance": 1234.5}`
#: sin contexto es un número que el modelo puede leer como cualquier cosa.
LIVE_ETIQUETAS: Dict[str, str] = {
    "account_info": "Saldo, equity, margen y nivel de margen de la cuenta",
    "positions_list": "Posiciones abiertas en MT5",
    "history": "Historial de operaciones de los últimos días",
    "economic_news": "Última y próxima noticia económica relevante",
}


# ---------------------------------------------------------------------------
# Enmarcado SSE
# ---------------------------------------------------------------------------


def sse(evento: Dict[str, Any]) -> str:
    """Un evento del stream, con el framing que el cliente ya sabe leer.

    `data:` y nada de `event:`. El tipo va DENTRO del JSON: el cliente parte el
    búfer por `\n\n`, descarta lo que no empieza por `data: ` y despacha por
    `payload.type`. Con líneas `event:` el `EventSource` nativo dejaría de recibir
    los eventos que no conoce, que son la mayoría.

    `ensure_ascii=False` a propósito: los textos del agente están en español y sin
    él cada acento son seis bytes de escape en el cable.
    """
    return "data: {0}\n\n".format(json.dumps(evento, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Router: nombres, claves y cadena
# ---------------------------------------------------------------------------


def normalize_model(model: Optional[str]) -> Optional[str]:
    """Nombre canónico de un modelo, o `None` si no hay ninguno."""
    limpio = str(model or "").strip()
    if not limpio:
        return None
    return LEGACY_MODEL_MAP.get(limpio, limpio)


def env_for_model(model: Optional[str]) -> Optional[str]:
    """Variable de entorno con la clave de este modelo, o `None` si es local."""
    prefijo = str(model or "").split("/")[0].lower()
    if prefijo == LOCAL_PREFIX:
        return None
    return PROVIDER_ENV.get(prefijo, "OPENAI_API_KEY")


def is_local_model(model: Optional[str]) -> bool:
    """¿El modelo es local (Ollama)? Cambia timeout, degradación y formato de salida."""
    return str(model or "").split("/")[0].lower() == LOCAL_PREFIX


def gemini_keys(env: Optional[Dict[str, str]] = None) -> List[str]:
    """Claves de Gemini disponibles, en orden. `env` inyectable para los tests."""
    fuente = os.environ if env is None else env
    claves = []
    for nombre in GEMINI_KEY_ENVS:
        valor = fuente.get(nombre)
        if valor and str(valor).strip():
            claves.append(str(valor).strip())
    return claves


def provider_tasks(
    chain: Sequence[str], env: Optional[Dict[str, str]] = None
) -> List[Tuple[str, Optional[str]]]:
    """Expande la cadena en pares `(modelo, clave)` que se intentan en orden.

    Un modelo sin clave no aparece: intentarlo produce un 401 que se confunde con
    "el proveedor está caído" y hace avanzar el fallback por el motivo equivocado.
    Gemini se expande a una tarea por clave, que es lo que permite que un 429 de
    cuota no tumbe al proveedor entero.
    """
    fuente = os.environ if env is None else env
    tareas: List[Tuple[str, Optional[str]]] = []
    for modelo in chain or ():
        nombre = normalize_model(modelo)
        if not nombre:
            continue
        if is_local_model(nombre):
            tareas.append((nombre, None))
            continue
        variable = env_for_model(nombre) or "OPENAI_API_KEY"
        if variable == "GEMINI_API_KEY":
            for clave in gemini_keys(fuente):
                tareas.append((nombre, clave))
        elif fuente.get(variable):
            tareas.append((nombre, None))
    return tareas


def _ajusta_lista(valor: Any) -> List[str]:
    """`fallback_order` llega como texto JSON desde `agent_settings`.

    Un `json.loads` a pelo puede reventar por una configuración corrupta, y reventar
    por eso es perder el chat entero. Si no se puede leer, se devuelve vacío y quien
    llama usa su default, que es el comportamiento honesto: la casa tiene un
    default escrito.
    """
    if isinstance(valor, (list, tuple)):
        return [str(v).strip() for v in valor if str(v or "").strip()]
    if not valor:
        return []
    try:
        datos = json.loads(str(valor))
    except (TypeError, ValueError):
        return []
    if not isinstance(datos, list):
        return []
    return [str(v).strip() for v in datos if str(v or "").strip()]


def _ajusta_int(valor: Any, por_defecto: int) -> int:
    """`history_limit` y `max_tool_rounds` llegan como texto. `int()` a pelo no vale."""
    try:
        if valor is None or isinstance(valor, bool):
            return por_defecto
        numero = int(str(valor).strip())
    except (TypeError, ValueError):
        return por_defecto
    return numero if numero > 0 else por_defecto


def resolve_model_chain(
    rol: Optional[Dict[str, Any]],
    settings: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
) -> List[str]:
    """La cadena definitiva de modelos, en orden de preferencia.

    El rol manda: `provider='auto'` y `model=''` significan "decide tú", que es lo
    que siembra `store`, así que por defecto no bloquean la política de la casa. Un
    rol con modelo explícito se usa SÓLO ese: es lo que un usuario pide cuando fija
    un modelo, y mezclarlo con el fallback devolvería respuestas de dos modelos
    distintos sin avisar.

    Después: `fallback_order` de `agent_settings`, filtrando los modelos sin clave
    disponible, y como último recurso `DEFAULT_CHAIN`.
    """
    if rol:
        provider = str(rol.get("provider") or "auto").strip().lower()
        modelo = normalize_model(rol.get("model"))
        if provider and provider != "auto" and modelo:
            if "/" not in modelo:
                canonico = PROVIDER_ALIAS.get(provider, provider)
                modelo = normalize_model("{0}/{1}".format(canonico, modelo))
            if modelo:
                return [modelo]
    cadena = [
        m
        for m in (normalize_model(x) for x in _ajusta_lista((settings or {}).get("fallback_order")))
        if m
    ]
    if not cadena:
        cadena = list(DEFAULT_CHAIN)
    fuente = os.environ if env is None else env
    disponibles = []
    for modelo in cadena:
        if is_local_model(modelo):
            disponibles.append(modelo)
            continue
        variable = env_for_model(modelo) or "OPENAI_API_KEY"
        if fuente.get(variable):
            disponibles.append(modelo)
    return disponibles or cadena


def is_rate_limit(exc: BaseException) -> bool:
    """¿Este fallo es de límite de peticiones y merece esperar?

    Se mira el CÓDIGO y no solo el texto: los proveedores cambian el mensaje (y
    Gemini devuelve 429 en `status_code`, OpenAI a veces lo mete en el cuerpo), y
    un `str(exc)` que contiene "429" también aparece dentro de un id de petición.
    """
    status = getattr(exc, "status_code", None)
    if status in (429, 503):
        return True
    texto = str(exc).lower()
    return any(
        marca in texto
        for marca in (
            "rate limit",
            "ratelimit",
            "429",
            "503",
            "quota",
            "high demand",
            "serviceunavailable",
            "overloaded",
            "try again later",
        )
    )


def backoff_s(intento: int) -> float:
    """Espera antes del reintento `intento` (1 = el primero). Exponencial y corta."""
    return BACKOFF_BASE_S * (2 ** max(0, intento - 1))


async def _duerme(deps: Optional[AgentDeps], segundos: float) -> None:
    """La espera del backoff, con `deps.sleep` como seam.

    El backoff es la única espera del bucle y la única que un test no puede pagar de
    verdad. Con `deps.sleep` inyectado, el test del 429 no tarda ni un segundo.
    """
    fn = getattr(deps, "sleep", None) if deps is not None else None
    if fn is None:
        await asyncio.sleep(segundos)
        return
    resultado = fn(segundos)
    if inspect.isawaitable(resultado):
        await resultado


# ---------------------------------------------------------------------------
# Qué símbolo y qué mercado
# ---------------------------------------------------------------------------


def detect_symbol(mensaje: str, asset_map: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """El símbolo que menciona el mensaje, o `""` si no menciona ninguno.

    Se busca el más largo de los conocidos: en "DAX y EURUSD" un barrido corto puede
    devolver `AU` de otra cosa, y el símbolo equivocado decide si el modelo habla de
    pips o de puntos.

    El desempate es ALFABÉTICO a propósito. Ordenar solo por longitud deja el orden
    de los símbolos de la misma longitud en manos del iterado de un `set`, que varía
    entre ejecuciones: el mismo mensaje se resolvería a un mercado u otro según el
    proceso, y con él las unidades del prompt. Eso no es un detalle: es la
    reproducibilidad de la respuesta.

    Si no hay ninguno NO se devuelve un símbolo por defecto. El `market_type` de un
    símbolo inventado decide las unidades del prompt, y ese default sería
    exactamente el error que `prompt_templates` evita a propósito.
    """
    texto = " {0} ".format(str(mensaje or "").upper())
    candidatos = set(asset_map or ())
    candidatos.update(pt.SIMBOLOS_FUERA_DEL_YAML)
    for simbolo in sorted(candidatos, key=lambda s: (-len(s), s)):
        if len(simbolo) < 3:
            continue
        if " {0} ".format(simbolo) in texto:
            return simbolo
    return ""


def _config_trading(deps: Optional[AgentDeps]) -> Dict[str, Any]:
    """La configuración de trading por el puerto, o `{}`.

    `store.get_trading_config()` lanza `ConfigUnavailable` mientras `strategy` no
    exista (Fase 6). Un chat que se cae porque falta la configuración de la Fase 6
    no sirve de nada, así que la ausencia degrada a "sin config" y las plantillas la
    Dicen en el prompt.
    """
    store = getattr(deps, "store", None) if deps is not None else None
    if store is None:
        return {}
    try:
        return store.get_trading_config() or {}
    except Exception:  # noqa: BLE001 - la ausencia se DICE en el prompt
        return {}


def _topics(deps: Optional[AgentDeps], market_type: str, mensaje: str) -> Dict[str, List[str]]:
    """Los topics que el mensaje MENCIONA, con las palabras de cada uno.

    `pt.topics_for` devuelve el catálogo entero; aquí se queda con los topics que de
    verdad aparecen en el texto. Sin ese filtro, "hola, ¿qué tal?" arrastraría las
    cuatro lecturas en vivo (cuenta, posiciones, historial y noticias) y el prompt
    empezaría con datos que nadie pidió.

    `store.get_agent_topics()` devuelve `{}` cuando el YAML no define topics, y un
    dict vacío NO es lo mismo que `None`: `topics_for` usa `DEFAULT_TOPICS` solo
    cuando recibe `None`. Sin esta línea, un YAML sin topics deja al modelo sin
    ninguna palabra clave y `menciona` no detecta nunca nada.
    """
    store = getattr(deps, "store", None) if deps is not None else None
    base: Optional[Dict[str, List[str]]] = None
    if store is not None:
        try:
            base = store.get_agent_topics() or None
        except Exception:  # noqa: BLE001 - sin topics del YAML, los de por defecto
            base = None
    catalogo = pt.topics_for(market_type, base)
    return {
        tema: palabras
        for tema, palabras in catalogo.items()
        if pt.menciona(mensaje, palabras)
    }


def _estado_diario(deps: Optional[AgentDeps]) -> Dict[str, Any]:
    """El estado de riesgo del día, o `{}` si el bróker no contesta.

    `daily_risk_state` devuelve su error DENTRO del dict (`{"error": …}`), no como
    excepción: eso lo convierte en estado, y `risk_policy_lines` lo redacta. Aquí solo
    se capturan excepciones de verdad (puerto ausente o reventón).
    """
    market = getattr(deps, "market", None) if deps is not None else None
    if market is None:
        return {}
    try:
        return market.daily_risk_state() or {}
    except Exception as exc:  # noqa: BLE001 - el fallo se le DICE al modelo
        return {"error": str(exc)}


def _relojes(deps: Optional[AgentDeps]) -> str:
    """UTC, hora del broker y día de trading, cada uno si se puede.

    El orden es fijo y el primero es UTC: el modelo razona en UTC (killzones, velas,
    timestamps del gráfico) y en el del broker solo contrasta con lo que ve en el
    terminal. Los contadores diarios cuentan por día de BROKER, así que sin esa
    línea el modelo no puede saber si el tope ya se reseteó.
    """
    market = getattr(deps, "market", None) if deps is not None else None
    broker = dia = None
    if market is not None:
        try:
            broker = market.broker_time()
        except Exception:  # noqa: BLE001 - sin hora de broker, no se inventa
            broker = None
        try:
            dia = market.trading_day()
        except Exception:  # noqa: BLE001 - sin día de trading, no se inventa
            dia = None
    return "\n".join(pt.clock_lines(None, broker, dia))


# ---------------------------------------------------------------------------
# Ruta simple: datos en vivo inyectados, sin herramientas
# ---------------------------------------------------------------------------


async def fetch_live_data(
    mensaje: str,
    permitidos: Iterable[str],
    deps: Optional[AgentDeps] = None,
    conversation_id: str = "",
    cfg: Optional[Dict[str, Any]] = None,
    topics: Optional[Dict[str, List[str]]] = None,
) -> str:
    """El bloque de datos consultados en vivo, o `""` si no hay nada que decir.

    Existe la ruta simple porque el function-calling de los modelos locales no es
    fiable: uno pequeño a veces emite el tool call como texto plano. Con los datos ya
    en el prompt, el modelo redacta sin llamar a nada.

    Un dato que no se pudo leer se escribe como NO DISPONIBLE **con su motivo**. La
    versión de REF escribía siempre "MT5 no responde", que es mentira cuando lo que
    falla es el calendario de noticias: el modelo leía "MT5 caído" y lo traducía a
    "no hay bróker" en la respuesta.
    """
    permitidos = set(permitidos or ())
    lineas = [_relojes(deps)]
    # `topics` son las PALABRAS de cada topic, y sus claves son los nombres de topic:
    # de ahí se sacan las herramientas de la ruta simple. Las claves se pasan como
    # lista porque `tools_for_topics` espera topics, no el catálogo de palabras.
    for nombre in toolbox.tools_for_topics(list(topics or ()), con_live=True):
        if nombre not in permitidos:
            continue
        resultado = await toolbox.execute_tool(
            nombre, dict(toolbox.ARGS_LIVE_DATA.get(nombre, {})), deps, conversation_id
        )
        etiqueta = LIVE_ETIQUETAS.get(nombre, nombre)
        if resultado.get("status") == STATUS_OK:
            carga = json.dumps(resultado.get("data"), ensure_ascii=False, default=str)
            lineas.append("- {0}: {1}".format(etiqueta, carga[:2500]))
        else:
            lineas.append(
                "- {0}: NO DISPONIBLE ({1})".format(
                    etiqueta, resultado.get("error") or resultado.get("status") or "sin motivo"
                )
            )
    politica = pt.risk_policy_lines(cfg or {}, _estado_diario(deps))
    if politica:
        lineas.extend(politica)
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Acumulación de una ronda
# ---------------------------------------------------------------------------


class _Ronda:
    """Lo que una ronda produce, para que el bucle decida sin anidar generadores.

    Un `async` generador no puede devolver un valor, así que la ronda se acumula en
    un objeto mutable que el bucle crea, pasa y lee. Es más feo que devolver una
    tupla, y evita el patrón de "generador que emite eventos y además escribe en un
    dict de fuera", que no se puede testear ronda por ronda.
    """

    __slots__ = ("assistant", "llamadas", "ok", "error")

    def __init__(self) -> None:
        self.assistant: Dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": {}}
        self.llamadas: List[Dict[str, Any]] = []
        self.ok = False
        self.error: Optional[str] = None


class _Intento:
    """Un intento concreto contra un par (modelo, clave).

    Separate de `_Ronda` porque los dos se reinician en bucles distintos: la ronda
    dura `max_tool_rounds` y el intento dura lo que tarde el proveedor. El texto
    bufferizado también es del intento: si el stream se corta, ese texto no puede
    aparecer en la ronda, porque no se sabe si era respuesta o tool call escrito.
    """

    __slots__ = ("modelo", "clave", "error", "excepcion", "buffer", "emitidos")

    def __init__(self, modelo: str, clave: Optional[str]) -> None:
        self.modelo = modelo
        self.clave = clave
        self.error: Optional[str] = None
        self.excepcion: Optional[BaseException] = None
        self.buffer: List[str] = []
        self.emitidos = 0


def _acumula(chunk: Any, ronda: _Ronda) -> str:
    """Vuelca un chunk del stream en la ronda y devuelve el texto que trae.

    Se aceptan las dos formas que usan los proveedores: `delta` (streaming normal) y
    `message` (algunos adaptadores entregan la respuesta completa en el último chunk).
    Ignorar la segunda hacía que ciertos proveedores devolvieran una ronda vacía y el
    agente respondiera "Sin respuesta textual del modelo" con la respuesta completa
    dentro.
    """
    choices = getattr(chunk, "choices", None)
    if not choices:
        return ""
    choice = choices[0]
    delta = getattr(choice, "delta", None)
    if delta is None:
        delta = getattr(choice, "message", None)
    if delta is None:
        return ""
    texto = ""
    contenido = getattr(delta, "content", None)
    if contenido:
        texto = str(contenido)
        ronda.assistant["content"] += texto
    for llamada in getattr(delta, "tool_calls", None) or ():
        indice = getattr(llamada, "index", None)
        idx = indice if indice is not None else 0
        slot = ronda.assistant["tool_calls"].setdefault(idx, {"id": None, "name": "", "arguments": ""})
        if getattr(llamada, "id", None):
            slot["id"] = llamada.id
        funcion = getattr(llamada, "function", None)
        if funcion is not None:
            if getattr(funcion, "name", None):
                slot["name"] += str(funcion.name)
            if getattr(funcion, "arguments", None):
                slot["arguments"] += str(funcion.arguments)
    return texto


def _llamadas_de(ronda: _Ronda) -> List[Dict[str, Any]]:
    """Los `tool_calls` acumulados, en orden de índice y con `id` siempre presente.

    Un proveedor puede no mandar `id`. Se genera uno aquí porque el mensaje de la
    ronda que se reenvía al modelo tiene que poder emparejarse con la respuesta de
    cada herramienta: si falta, la API rechaza el request entero.
    """
    salida = []
    for idx, slot in sorted(ronda.assistant["tool_calls"].items()):
        nombre = str(slot.get("name") or "").strip()
        if not nombre:
            continue
        salida.append(
            {
                "id": slot.get("id") or "call_{0}".format(idx),
                "function": {"name": nombre, "arguments": slot.get("arguments") or "{}"},
            }
        )
    return salida


def parse_text_tool_call(
    texto: str,
    permitidos: Iterable[str],
    now_epoch: Optional[Callable[[], float]] = None,
) -> Optional[Dict[str, Any]]:
    """Un tool call que el modelo escribió como texto JSON, o `None`.

    Los modelos pequeños emiten `{"name": …, "arguments": …}` en el contenido en
    lugar de en `tool_calls`. Sin esto, esa respuesta es texto inútil para el
    usuario. Solo se acepta si el texto es EXACTAMENTE un objeto JSON con un nombre
    permitido: interpretar como herramienta cualquier frase que tenga llaves es la
    forma más rápida de que el agente ejecute cosas que nadie pidió.
    """
    if not texto:
        return None
    permitidos = set(permitidos or ())
    limpio = str(texto).strip()
    if not (limpio.startswith("{") and limpio.endswith("}")):
        return None
    try:
        datos = json.loads(limpio)
    except (TypeError, ValueError):
        return None
    if not isinstance(datos, dict):
        return None
    nombre = datos.get("name")
    if not isinstance(nombre, str) or nombre.strip() not in permitidos:
        return None
    argumentos = datos.get("arguments", {})
    if isinstance(argumentos, dict):
        argumentos_json = json.dumps(argumentos, ensure_ascii=False)
    elif isinstance(argumentos, str):
        argumentos_json = argumentos
    else:
        argumentos_json = "{}"
    if now_epoch is None:
        from core import clock  # noqa: PLC0415 - tardío a propósito

        now_epoch = clock.epoch
    return {
        # epoch UTC y no `datetime.now().timestamp()`: sobre un datetime naive eso
        # interpreta la hora como local, y en un SO en UTC-3 devuelve un id tres horas
        # por delante del reloj real.
        "id": "call_local_{0}".format(int(now_epoch() * 1000)),
        "function": {"name": nombre.strip(), "arguments": argumentos_json},
    }


async def _pide_al_proveedor(
    intento: _Intento,
    ronda: _Ronda,
    mensajes: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    completion: Callable[..., Awaitable[Any]],
    emitir_en_vivo: bool,
) -> AsyncIterator[str]:
    """Un intento contra el par (modelo, clave) del `intento`. Emite los deltas.

    Deja el error en `intento.error` y `intento.excepcion`; el bucle decide qué hacer
    con él. Cada fallo tiene su propio texto para el usuario ("límite de peticiones"
    no es "conexión interrumpida") y distinguirlos por el camino de salida sería más
    barroco que mirar la excepción.
    """
    kwargs: Dict[str, Any] = {}
    if intento.clave:
        kwargs["api_key"] = intento.clave
    try:
        stream_ = await completion(
            model=intento.modelo,
            messages=mensajes,
            tools=tools or None,
            stream=True,
            timeout=TIMEOUT_S_LOCAL if is_local_model(intento.modelo) else TIMEOUT_S_REMOTO,
            # El reintento es de este bucle, no de LiteLLM: cada reintento nuestro
            # puede rotar de clave, y los internos no rotan.
            max_retries=0,
            **kwargs
        )
    except Exception as exc:  # noqa: BLE001 - el fallo se le DICE al usuario
        intento.excepcion = exc
        intento.error = str(exc)
        return
    try:
        async for chunk in stream_:
            texto = _acumula(chunk, ronda)
            if not texto:
                continue
            if emitir_en_vivo:
                intento.emitidos += 1
                yield texto
            else:
                intento.buffer.append(texto)
    except Exception as exc:  # noqa: BLE001 - stream cortado a medias
        intento.excepcion = exc
        intento.error = str(exc)


# ---------------------------------------------------------------------------
# El puente
# ---------------------------------------------------------------------------


async def _completion_por_defecto(**kwargs: Any) -> Any:
    """El `completion` real: LiteLLM, importado en la llamada y no en el módulo."""
    from litellm import acompletion  # noqa: PLC0415 - tardío a propósito (ver docstring)

    return await acompletion(**kwargs)


def _resolver_rol(store: Any, role_id: str) -> Optional[Dict[str, Any]]:
    """El rol pedido y, si no existe, el de `general`."""
    if store is None:
        return None
    return store.get_role(role_id) or store.get_role("general")


def _settings_de(store: Any) -> Dict[str, Any]:
    if store is None:
        return {}
    try:
        return store.get_settings() or {}
    except Exception:  # noqa: BLE001 - sin settings, los defaults de este módulo
        return {}


def _persistir(store: Any, conversation_id: str, role: str, content: str,
               tool_call_id: Optional[str] = None, name: Optional[str] = None) -> Optional[str]:
    """Guarda un mensaje. Devuelve el texto de error, o `None` si se guardó.

    Que la persistencia devuelva el error en vez de dejarlo caer no es un capricho:
    `messages` es la memoria del chat y una fila que no se escribe es una amnesia que
    el usuario no ve. Se avisa por `status` en vez de reventar el stream, porque la
    respuesta ya está en pantalla y perderla sería peor.
    """
    if store is None:
        return None
    try:
        store.add_message(conversation_id, role, content, tool_call_id=tool_call_id, name=name)
    except Exception as exc:  # noqa: BLE001 - la amnesia se DICE
        return str(exc)
    return None


def _mensaje_usuario(mensaje: str, chart_image: Optional[str]) -> Dict[str, Any]:
    """El turno del usuario, con la instrucción de idioma y la imagen si viene.

    La instrucción de idioma va pegada al mensaje y no al system prompt: es una
    instrucción de ESTE turno y su sitio natural es junto a lo que se responde.

    Una imagen puede venir como base64 crudo (lo que entrega el endpoint de captura)
    o como URL (lo que devuelve el backend de imágenes). Solo lo primero se envuelve
    en un `data:`: prefijar una URL deja `data:image/png;base64,https://…`, que no es
    una imagen y el proveedor rechaza el request entero.
    """
    contenido = "{0}\n\nResponde siempre en español.".format(mensaje)
    if chart_image:
        url = str(chart_image).strip()
        if not url.lower().startswith(("data:", "http://", "https://")):
            url = "data:image/png;base64,{0}".format(url)
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": contenido},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    return {"role": "user", "content": contenido}


async def _cuerpo(
    conversation_id: str,
    role_id: str,
    mensaje: str,
    deps: Optional[AgentDeps] = None,
    skip_tools: bool = False,
    chart_image: Optional[str] = None,
    completion: Optional[Callable[..., Awaitable[Any]]] = None,
    env: Optional[Dict[str, str]] = None,
    asset_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> AsyncIterator[str]:
    """El bucle. NO emite `done`: eso es de `stream`, y solo de `stream`."""
    store = getattr(deps, "store", None) if deps is not None else None
    completar: Callable[..., Awaitable[Any]] = completion or _completion_por_defecto

    rol = _resolver_rol(store, role_id)
    if not rol:
        yield sse({"type": EVENTO_ERROR, "content": "No hay ningún rol de agente configurado."})
        return

    settings = _settings_de(store)
    history_limit = _ajusta_int(settings.get("history_limit"), DEFAULT_HISTORY_LIMIT)
    max_rounds = _ajusta_int(settings.get("max_tool_rounds"), DEFAULT_MAX_TOOL_ROUNDS)

    mapa = pt.load_asset_map() if asset_map is None else asset_map
    simbolo = detect_symbol(mensaje, mapa)
    market_type = pt.market_for(simbolo, mapa) if simbolo else pt.DESCONOCIDO
    cfg = _config_trading(deps)
    topics = _topics(deps, market_type, mensaje)
    permitidos_rol = set(rol.get("allowed_tools") or ())

    yield sse({"type": "role", "content": rol.get("name") or role_id})
    yield sse({"type": "status", "content": "Iniciando agente «{0}»".format(rol.get("name") or role_id)})

    cadena = resolve_model_chain(rol, settings, env)
    tareas = provider_tasks(cadena, env)
    if not tareas:
        yield sse(
            {
                "type": EVENTO_ERROR,
                "content": "No hay claves de API configuradas (OPENAI_API_KEY, GEMINI_API_KEY o "
                "ANTHROPIC_API_KEY) ni un modelo local disponible.",
            }
        )
        return

    # Degradación controlada: un modelo local con function-calling poco fiable baja a
    # ruta simple. Se dice en voz alta porque el usuario pidió herramientas y va a
    # recibir una respuesta redactada sin ellas.
    quiere_tools = bool(pt.is_complex_request(mensaje, market_type) and not skip_tools)
    if quiere_tools and is_local_model(tareas[0][0]):
        yield sse(
            {
                "type": "status",
                "content": "Modelo local: degradando a modo simple (sin herramientas) con datos en vivo",
            }
        )
        quiere_tools = False

    schemas = toolbox.tool_schemas(sorted(permitidos_rol)) if quiere_tools and permitidos_rol else None

    mensajes: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": pt.system_prompt(
                rol.get("system_prompt"),
                market_type,
                cfg,
                (mapa or {}).get(simbolo) if simbolo else None,
            ),
        }
    ]
    reloj = _relojes(deps)
    if reloj:
        mensajes.append({"role": "system", "content": reloj})

    # El historial se lee ANTES de guardar el turno del usuario. Al revés, la pregunta
    # llega dos veces al modelo: como historial y como mensaje final, y el LLM ve su
    # propia pregunta repetida justo cuando tiene que responderla.
    if store is not None:
        for previo in store.get_messages(conversation_id, limit=history_limit) or []:
            mensajes.append({"role": previo.get("role") or "user", "content": previo.get("content") or ""})
    mensajes.append(_mensaje_usuario(mensaje, chart_image))
    _persistir(store, conversation_id, "user", mensaje)

    if not quiere_tools and not skip_tools:
        yield sse({"type": "status", "content": "Consultando datos en vivo de la cuenta"})
        live = await fetch_live_data(mensaje, permitidos_rol, deps, conversation_id, cfg, topics)
        if live:
            mensajes.append(
                {
                    "role": "system",
                    "content": pt.live_data_block([live], pt.data_sources_policy(cfg)),
                }
            )

    for round_no in range(1, max_rounds + 1):
        ronda = _Ronda()
        intento_ok: Optional[_Intento] = None
        error_final: Optional[str] = None

        for modelo, clave in tareas:
            # Los modelos locales se BUFFERIZAN: su respuesta puede ser un tool call
            # escrito como texto, y emitirlo como `delta` lo enseñaría al usuario
            # antes de saber si es una respuesta o una llamada.
            en_vivo = not is_local_model(modelo)
            intentos = 0
            while True:
                intentos += 1
                intento = _Intento(modelo, clave)
                ronda.assistant["content"] = ""
                ronda.assistant["tool_calls"] = {}
                yield sse({"type": "model", "content": modelo})
                async for delta in _pide_al_proveedor(intento, ronda, mensajes, schemas, completar, en_vivo):
                    yield sse({"type": "delta", "content": delta})
                if intento.error is None:
                    intento_ok = intento
                    break
                # El stream se cortó DESPUÉS de emitir texto: no se cambia de
                # proveedor. El usuario ya tiene media respuesta en pantalla y
                # encadenarle la de otro modelo produce dos respuestas superpuestas.
                if intento.emitidos:
                    error_final = "cortado con texto ya emitido: {0}".format(intento.error)
                    break
                if (
                    intento.excepcion is not None
                    and is_rate_limit(intento.excepcion)
                    and intentos < MAX_INTENTOS_LIMITE
                ):
                    espera = backoff_s(intentos)
                    yield sse(
                        {
                            "type": "status",
                            "content": "Límite de peticiones en {0}: reintento en {1:.1f}s".format(modelo, espera),
                        }
                    )
                    await _duerme(deps, espera)
                    continue
                yield sse(
                    {
                        "type": "status",
                        "content": "Fallo {0}: {1}. Probando siguiente clave o proveedor".format(
                            modelo, intento.error
                        ),
                    }
                )
                break
            if intento_ok is not None or error_final:
                break

        if intento_ok is None:
            detalle = error_final or "sin proveedores con clave configurada"
            yield sse({"type": EVENTO_ERROR, "content": "Todos los proveedores fallaron ({0}).".format(detalle)})
            fallo = _persistir(store, conversation_id, "assistant", SIN_RESPUESTA)
            if fallo:
                yield sse({"type": "status", "content": "No se pudo guardar la respuesta: {0}".format(fallo)})
            return

        llamadas = _llamadas_de(ronda)
        if not llamadas and is_local_model(intento_ok.modelo):
            # Un modelo local puede responder con el tool call ESCRITO en el texto.
            # El buffer se vacía porque ese texto no era la respuesta: era la llamada.
            escrita = parse_text_tool_call(
                ronda.assistant["content"], permitidos_rol, deps.now if deps else None
            )
            if escrita:
                llamadas = [escrita]
                ronda.assistant["content"] = ""
                intento_ok.buffer = []

        if not llamadas:
            for trozo in intento_ok.buffer:
                yield sse({"type": "delta", "content": trozo})
            texto = ronda.assistant["content"].strip() or SIN_RESPUESTA
            fallo = _persistir(store, conversation_id, "assistant", texto)
            if fallo:
                yield sse({"type": "status", "content": "No se pudo guardar la respuesta: {0}".format(fallo)})
            return

        ronda.assistant["content"] = ronda.assistant["content"] or None
        ronda.assistant["tool_calls"] = llamadas
        mensajes.append(ronda.assistant)

        for llamada in llamadas:
            nombre = llamada["function"]["name"]
            argumentos = toolbox.parse_args(llamada["function"]["arguments"])
            yield sse({"type": "tool", "content": "Consultando {0}".format(nombre)})
            inicio = deps.now() if deps is not None and deps.now is not None else None
            if nombre not in permitidos_rol:
                resultado: Dict[str, Any] = {
                    "status": STATUS_FAILED,
                    "data": None,
                    "error": "herramienta no permitida para este rol",
                }
            else:
                resultado = await toolbox.execute_tool(nombre, argumentos, deps, conversation_id, round_no)
            elapsed = None
            if inicio is not None and deps is not None and deps.now is not None:
                elapsed = max(0.0, (deps.now() - inicio) * 1000.0)
            if resultado.get("status") == STATUS_OK:
                if nombre == "set_chart_alert":
                    yield sse({"type": "chart_alert", "content": resultado.get("data")})
                elif nombre == "chart_annotate":
                    yield sse({"type": "chart_actions", "content": resultado.get("data")})
            # A la traza, el sobre con args, estado y tamaño. Al modelo, el payload
            # COMPLETO: recortar la respuesta que lee el modelo es inventarse un dato.
            fallo = _persistir(
                store,
                conversation_id,
                "tool",
                pt.tool_digest(nombre, argumentos, resultado, round_no, elapsed),
                tool_call_id=llamada["id"],
                name=nombre,
            )
            if fallo:
                yield sse({"type": "status", "content": "No se pudo guardar la traza de {0}: {1}".format(nombre, fallo)})
            mensajes.append(
                {
                    "role": "tool",
                    "tool_call_id": llamada["id"],
                    "name": nombre,
                    "content": json.dumps(resultado, ensure_ascii=False, default=str),
                }
            )

    fallo = _persistir(store, conversation_id, "assistant", RONDAS_EXCESO)
    if fallo:
        yield sse({"type": "status", "content": "No se pudo guardar la respuesta: {0}".format(fallo)})
    yield sse(
        {"type": EVENTO_ERROR, "content": "Demasiadas rondas de herramientas. Respuesta interrumpida."}
    )


async def stream(
    conversation_id: str,
    role_id: str,
    mensaje: str,
    deps: Optional[AgentDeps] = None,
    skip_tools: bool = False,
    chart_image: Optional[str] = None,
    completion: Optional[Callable[..., Awaitable[Any]]] = None,
    env: Optional[Dict[str, str]] = None,
    asset_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> AsyncIterator[str]:
    """El stream del agente. Emite `done` UNA vez, y solo si nadie se desconectó.

    El `done` vive aquí y no en `_cuerpo` por dos razones concretas:

    - REF lo emitía en cinco ramas del bucle. Una sexta rama nueva lo olvidaba y el
      cliente se quedaba esperando un cierre que no llegaba.
    - Ponerlo en un `finally` tampoco vale: si el cliente desconecta, el generador
      recibe `GeneratorExit` y un `yield` dentro del `finally` revienta con "async
      generator ignored GeneratorExit". Con esta forma la desconexión propaga el
      `GeneratorExit` y no se emite nada, que es lo correcto: no hay nadie escuchando.
    """
    try:
        async for evento in _cuerpo(
            conversation_id,
            role_id,
            mensaje,
            deps,
            skip_tools=skip_tools,
            chart_image=chart_image,
            completion=completion,
            env=env,
            asset_map=asset_map,
        ):
            yield evento
    except Exception as exc:  # noqa: BLE001 - al cliente se le dice, no se le rompe
        yield sse({"type": EVENTO_ERROR, "content": "Fallo del agente: {0}".format(exc)})
    yield sse({"type": EVENTO_DONE})


__all__ = [
    "BACKOFF_BASE_S",
    "DEFAULT_CHAIN",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_MAX_TOOL_ROUNDS",
    "EVENTO_DONE",
    "EVENTO_ERROR",
    "GEMINI_KEY_ENVS",
    "LEGACY_MODEL_MAP",
    "LIVE_ETIQUETAS",
    "LOCAL_PREFIX",
    "MAX_INTENTOS_LIMITE",
    "PROVIDER_ALIAS",
    "PROVIDER_ENV",
    "RONDAS_EXCESO",
    "SIN_RESPUESTA",
    "TIMEOUT_S_LOCAL",
    "TIMEOUT_S_REMOTO",
    "backoff_s",
    "detect_symbol",
    "fetch_live_data",
    "gemini_keys",
    "is_local_model",
    "is_rate_limit",
    "normalize_model",
    "parse_text_tool_call",
    "provider_tasks",
    "resolve_model_chain",
    "sse",
    "stream",
]