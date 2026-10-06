"""Tests de `agent/laya_bridge.py`: el router, el bucle de herramientas y el stream.

Qué fijan estos tests
---------------------
No "que devuelve algo": fijan las decisiones que en REF se tomaban en cinco sitios
distintos y que nadie podía ver en un test.

- **`done` es un invariante del stream, no una etiqueta por rama.** Un `yield` de
  más y el cliente se queda con el stream abierto para siempre; uno de menos y
  creye que la respuesta terminó cuando no. Se cuenta en todos los caminos,
  incluido el de error.
- **El fallback para antes del primer `delta` y nunca después.** Es la diferencia
  entre una respuesta y dos respuestas superpuestas en pantalla.
- **El 429 se reintenta en la MISMA clave con backoff, no rotando.** Con `deps.sleep`
  inyectado, el test del reintento no tarda ni un milisegundo.
- **La traza y el modelo no ven lo mismo, a propósito.** A la base va el digest con
  argumentos; al modelo va el payload completo.
- **El historial se lee antes de persistir el turno.** Al revés, la pregunta llega
  dos veces al modelo.
- **Un tool call ESCRITO por un modelo local no se le enseña al usuario.** Se
  ejecuta y se vacía el búfer, porque ese texto no era respuesta.

Por qué no se toca la red
-------------------------
`completion` es un seam: el test inyecta un doble y lo que se prueba es la lógica
del bucle. Probarlo contra Gemini sería probar la red.
"""

from __future__ import annotations

import asyncio
import ast
import json
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from agent import laya_bridge as LB


# ---------------------------------------------------------------------------
# Chunk de proveedor
# ---------------------------------------------------------------------------


class _Delta:
    def __init__(self, content: Optional[str], tool_calls: Optional[List[Any]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta: _Delta) -> None:
        self.delta = delta


class Chunk:
    """Un chunk del stream, con la forma que usa LiteLLM."""

    def __init__(self, content: Optional[str] = None, tool_calls: Optional[List[Any]] = None) -> None:
        self.choices = [_Choice(_Delta(content, tool_calls))]


class _Funcion:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class ToolCall:
    def __init__(self, index: int, name: str, arguments: str, call_id: Optional[str] = None) -> None:
        self.index = index
        self.id = call_id
        self.function = _Funcion(name, arguments)


class Boom(Exception):
    """Fallo de proveedor con código, para probar el 429 sin depender del texto."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Dobles de puerto
# ---------------------------------------------------------------------------


ROL = {
    "name": "General",
    "system_prompt": "Eres conciso.",
    "allowed_tools": ["account_info", "now"],
    "provider": "auto",
    "model": "",
}

ROL_LOCAL = dict(ROL, provider="ollama", model="llama3.2:3b")

MAPA = {"EURUSD": {"market_type": "forex", "digits": 5, "point": 0.00001, "pip": 0.0001}}

PREGUNTA_COMPLEJA = "analiza el EURUSD y dime si hay entrada con stoploss y takeprofit"


class StoreFake:
    """`StorePort` en memoria, con registro del ORDEN de las llamadas.

    El orden importa en un test y en producción: si el historial se lee después de
    guardar el turno, el modelo ve la pregunta dos veces. Un doble que solo guardara
    los mensajes no lo detectaría.
    """

    def __init__(
        self,
        roles: Optional[Dict[str, Dict[str, Any]]] = None,
        settings: Optional[Dict[str, Any]] = None,
        historial: Optional[List[Dict[str, Any]]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.roles = roles if roles is not None else {"general": dict(ROL)}
        self.settings = settings if settings is not None else {}
        self.historial = historial or []
        self.config = config if config is not None else {}
        self.mensajes: List[Dict[str, Any]] = []
        self.orden: List[str] = []
        self.journal: List[Dict[str, Any]] = []

    def get_role(self, role_id: str) -> Optional[Dict[str, Any]]:
        return self.roles.get(role_id)

    def get_settings(self) -> Dict[str, Any]:
        return dict(self.settings)

    def get_messages(self, conversation_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        self.orden.append("get_messages")
        return list(self.historial)

    def add_message(self, conversation_id, role, content, tool_call_id=None, name=None) -> None:
        self.orden.append("add_message:{0}".format(role))
        self.mensajes.append(
            {"role": role, "content": content, "tool_call_id": tool_call_id, "name": name}
        )

    def add_journal_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        self.journal.append(dict(entry))
        return self.journal[-1]

    def get_agent_topics(self) -> Dict[str, List[str]]:
        return {}

    def get_trading_config(self) -> Dict[str, Any]:
        return dict(self.config)

    def de(self, role: str) -> List[Dict[str, Any]]:
        return [m for m in self.mensajes if m["role"] == role]


class MarketFake:
    """`MarketPort` mínimo, con memoria de lo que se le pidió."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def account_info(self) -> Dict[str, Any]:
        self.calls.append("account_info")
        return {"balance": 10_000.0, "equity": 10_120.5}

    def positions(self) -> List[Dict[str, Any]]:
        self.calls.append("positions")
        return [{"symbol": "EURUSD", "action": "BUY", "volume": 0.1}]

    def history(self, days: int) -> List[Dict[str, Any]]:
        self.calls.append("history")
        return [{"ticket": 1, "days": days}]

    def price(self, symbol: str) -> Optional[Dict[str, Any]]:
        self.calls.append("price")
        return {"symbol": symbol, "bid": 1.1650, "ask": 1.1652}

    def pattern_data(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        self.calls.append("pattern_data")
        return None

    def chart_snapshot(self, symbol: str, timeframe: str) -> Dict[str, Any]:
        self.calls.append("chart_snapshot")
        return {}

    def orderflow_snapshot(self) -> Dict[str, Any]:
        self.calls.append("orderflow_snapshot")
        return {}

    def orderflow_alerts(self) -> List[Dict[str, Any]]:
        self.calls.append("orderflow_alerts")
        return []

    def daily_risk_state(self) -> Dict[str, Any]:
        self.calls.append("daily_risk_state")
        return {"trades_today": 1, "blocked": False}

    def enrich_journal_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        return entry

    def broker_time(self) -> str:
        return "2026-01-05 12:00:00 (broker)"

    def trading_day(self) -> str:
        return "2026-01-05"


# ---------------------------------------------------------------------------
# El seam `completion`
# ---------------------------------------------------------------------------


class CompletionFake:
    """El `completion` inyectado: reparte el guion en orden, repetir el último paso.

    Un paso puede ser una `Boom` (falla al pedir), una lista de chunks (stream) o
    una función que recibe los kwargs del bucle y devuelve los chunks: lo último es
    lo que permite afirmar sobre lo que se envió sin needing otro doble.
    """

    def __init__(self, guion: Any) -> None:
        self.guion = list(guion)
        self.calls: List[Dict[str, Any]] = []

    @property
    def n_llamadas(self) -> int:
        return len(self.calls)

    def modelos(self) -> List[str]:
        return [str(c.get("model")) for c in self.calls]

    def claves(self) -> List[Any]:
        return [c.get("api_key") for c in self.calls]

    def mensajes_de(self, llamada: int) -> List[Dict[str, Any]]:
        return list(self.calls[llamada].get("messages") or [])

    def texto_de(self, llamada: int, role: str) -> str:
        return "\n".join(
            str(m.get("content")) for m in self.mensajes_de(llamada) if m.get("role") == role
        )

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        paso = self.guion[min(len(self.calls) - 1, len(self.guion) - 1)]

        async def _devuelve() -> AsyncIterator[Any]:
            if isinstance(paso, BaseException):
                raise paso
            chunks = paso(kwargs) if callable(paso) else paso
            if isinstance(chunks, BaseException):
                raise chunks
            if hasattr(chunks, "__aiter__"):
                async for chunk in chunks:
                    yield chunk
                return
            for chunk in chunks:
                yield chunk

        return _devuelve()


class SleepFake:
    def __init__(self) -> None:
        self.esperas: List[float] = []

    def __call__(self, segundos: float) -> None:
        self.esperas.append(segundos)


class StoreRoto(StoreFake):
    """Un disco lleno: la persistencia falla y el chat tiene que seguir."""

    def add_message(self, conversation_id, role, content, tool_call_id=None, name=None) -> None:
        raise RuntimeError("disco lleno")


# ---------------------------------------------------------------------------
# Utilidades de stream
# ---------------------------------------------------------------------------


def eventos(chunks: List[str]) -> List[Dict[str, Any]]:
    return [json.loads(c[len("data: "):]) for c in chunks]


def de_tipo(lista: List[Dict[str, Any]], tipo: str) -> List[Dict[str, Any]]:
    return [e for e in lista if e.get("type") == tipo]


def texto_delta(lista: List[Dict[str, Any]]) -> str:
    return "".join(str(e.get("content")) for e in de_tipo(lista, "delta"))


def deps_de(
    store: Optional[StoreFake] = None,
    market: Any = None,
    sleep: Optional[SleepFake] = None,
) -> Any:
    from agent.ports import AgentDeps

    return AgentDeps(market=market, store=store, sleep=sleep, now=lambda: 1_700_000_000.0)


def correr(gen_factory: Any) -> List[Dict[str, Any]]:
    """Agota el stream en un loop nuevo y devuelve los eventos parseados."""

    async def _run() -> List[Dict[str, Any]]:
        return eventos([c async for c in gen_factory()])

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Enmarcado SSE
# ---------------------------------------------------------------------------


def test_sse_usa_framing_data_y_no_event():
    crudo = LB.sse({"type": "delta", "content": "hola"})
    assert crudo.startswith("data: ")
    assert crudo.endswith("\n\n")
    # Con líneas `event:` el `EventSource` nativo dejaría fuera lo que no conoce, que
    # es casi todo: el tipo va dentro del JSON y el cliente despacha por `payload.type`.
    assert "event:" not in crudo


def test_sse_no_escapa_los_acentos():
    assert "señal" in LB.sse({"type": "delta", "content": "señal"})


def test_litellm_solo_se_importa_dentro_de_una_funcion():
    # D-017: `import agent` no puede abrir una sesión de MT5 ni cargar un cliente de
    # LLM. El seam `completion` existe justo para eso, así que un import arriba del todo
    # del módulo rompería el invariante sin que ningún test funcional lo notase.
    arbol = ast.parse(Path(LB.__file__).read_text(encoding="utf-8"))

    def raices(nodo: ast.AST) -> set:
        if isinstance(nodo, ast.Import):
            return {alias.name.split(".")[0] for alias in nodo.names}
        if isinstance(nodo, ast.ImportFrom):
            return {(nodo.module or "").split(".")[0]}
        return set()

    prohibidos = {"litellm", "app", "MetaTrader5"}
    assert not prohibidos & {r for nodo in arbol.body for r in raices(nodo)}
    tardios = [nodo for nodo in ast.walk(arbol) if isinstance(nodo, (ast.Import, ast.ImportFrom))]
    assert any(prohibidos & raices(nodo) for nodo in tardios[1:]), (
        "el import de litellm debe existir, y dentro de la llamada"
    )


# ---------------------------------------------------------------------------
# Router: nombres, claves y cadena
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("gemini/gemini-3.6-flash", "gemini/gemini-3.8-flash"),
        ("google/gemini-3.7-flash", "gemini/gemini-3.8-flash"),
        ("gemini/gemini-3.8-flash", "gemini/gemini-3.8-flash"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_model_mapea_legados(entrada, esperado):
    assert LB.normalize_model(entrada) == esperado


def test_resolve_model_chain_un_rol_con_modelo_manda():
    cadena = LB.resolve_model_chain(
        {"provider": "openai", "model": "gpt-4o-mini"},
        {"fallback_order": '["gemini/gemini-3.8-flash"]'},
        {"OPENAI_API_KEY": "k", "GEMINI_API_KEY": "k2"},
    )
    # Sin fallback: encadenar dos modelos distintos daría dos respuestas distintas
    # sin avisar de que el usuario fijó uno.
    assert cadena == ["openai/gpt-4o-mini"]


def test_resolve_model_chain_auto_usa_fallback_order():
    cadena = LB.resolve_model_chain(
        {"provider": "auto", "model": ""},
        {"fallback_order": '["gemini/gemini-3.6-flash"]'},
        {"GEMINI_API_KEY": "k"},
    )
    assert cadena == ["gemini/gemini-3.8-flash"]


def test_resolve_model_chain_filtra_lo_que_no_tiene_clave():
    cadena = LB.resolve_model_chain(
        {"provider": "auto", "model": ""},
        {"fallback_order": '["gemini/gemini-3.8-flash", "openai/gpt-4o-mini"]'},
        {"GEMINI_API_KEY": "k"},
    )
    assert cadena == ["gemini/gemini-3.8-flash"]


def test_resolve_model_chain_sin_configuracion_usa_la_casa():
    assert LB.resolve_model_chain({"provider": "auto", "model": ""}, {}, {}) == list(LB.DEFAULT_CHAIN)


def test_resolve_model_chain_ignora_un_fallback_order_corrupto():
    # Una fila de settings a medio escribir no puede reventar el chat entero.
    cadena = LB.resolve_model_chain(
        {"provider": "auto", "model": ""}, {"fallback_order": "[no es json"}, {}
    )
    assert cadena == list(LB.DEFAULT_CHAIN)


def test_resolve_model_chain_traduce_un_alias_de_proveedor():
    assert LB.resolve_model_chain({"provider": "claude", "model": "sonnet"}, {}, {}) == [
        "anthropic/sonnet"
    ]


def test_provider_tasks_expande_gemini_por_clave():
    tareas = LB.provider_tasks(
        ("gemini/gemini-3.8-flash",), {"GEMINI_API_KEY": "a", "GEMINI_API_KEY_2": "b"}
    )
    # Un 429 de cuota es de la CLAVE: sin esta expansión el fallback avanzaría al
    # modelo siguiente del mismo proveedor y se perdería la causa del fallo.
    assert tareas == [("gemini/gemini-3.8-flash", "a"), ("gemini/gemini-3.8-flash", "b")]


def test_provider_tasks_omite_el_modelo_sin_clave():
    # Un 401 se confunde con "el proveedor está caído" y hace avanzar el fallback
    # por el motivo equivocado.
    assert LB.provider_tasks(("openai/gpt-4o-mini",), {}) == []


def test_provider_tasks_un_local_no_necesita_clave():
    assert LB.provider_tasks(("ollama/llama3.2:3b",), {}) == [("ollama/llama3.2:3b", None)]


def test_gemini_keys_descarta_vacios():
    assert LB.gemini_keys({"GEMINI_API_KEY": "  ", "GEMINI_API_KEY_2": " b "}) == ["b"]


@pytest.mark.parametrize(
    "exc,esperado",
    [
        (Boom("quota", status_code=429), True),
        (Boom("sin nada que ver"), False),
        (Boom("Rate limit reached for model"), True),
        (Boom("connection reset by peer"), False),
    ],
)
def test_is_rate_limit_mira_el_codigo_y_el_texto(exc, esperado):
    assert LB.is_rate_limit(exc) is esperado


def test_backoff_crece_y_no_es_negativo():
    esperas = [LB.backoff_s(i) for i in (1, 2, 3, 0)]
    assert esperas[0] < esperas[1] < esperas[2]
    assert esperas[3] == LB.BACKOFF_BASE_S


# ---------------------------------------------------------------------------
# Qué símbolo
# ---------------------------------------------------------------------------


def test_detect_symbol_prefiere_el_mas_largo():
    assert LB.detect_symbol("analiza el XAUUSD", {"EURUSD": {}, "XAUUSD": {}}) == "XAUUSD"


def test_detect_symbol_desempate_alfabetico():
    # DAX y SPX miden lo mismo y salen ambos del catálogo. Sin desempate, el resultado
    # dependería del iterado del set —y con él el `market_type` que decide si el modelo
    # habla de pips o de puntos—, así que el mismo mensaje acabaría en un mercado u
    # otro según el proceso.
    assert LB.detect_symbol("analiza el SPX y el DAX", {"SPX": {}}) == "DAX"
    assert LB.detect_symbol("analiza el SPX y el DAX", {"SPX": {}, "DAX": {}}) == "DAX"


def test_detect_symbol_sin_simbolo_devuelve_vacio():
    # Un símbolo inventado por defecto haría que el prompt anunciara pips en un
    # mercado donde no los hay.
    assert LB.detect_symbol("¿qué tal todo?", {}) == ""


def test_detect_symbol_no_se_confunde_con_una_palabra():
    # Sin el espacio protector, "EURUSD" dentro de otra palabra sería un acierto.
    assert LB.detect_symbol("el precio de XAUUSDTT no lo veo", {"XAUUSD": {}}) == ""


# ---------------------------------------------------------------------------
# Tool call escrito por un modelo local
# ---------------------------------------------------------------------------


def test_parse_text_tool_call_acepta_un_json_exacto():
    llamada = LB.parse_text_tool_call('{"name": "now", "arguments": {}}', ["now"], lambda: 1000.0)
    assert llamada is not None
    assert llamada["function"]["name"] == "now"
    assert llamada["id"] == "call_local_1000000"


def test_parse_text_tool_call_rechaza_una_herramienta_no_permitida():
    assert LB.parse_text_tool_call('{"name": "mt5_export_read", "arguments": {}}', ["now"]) is None


def test_parse_text_tool_call_no_interpreta_prosa():
    # Interpretar como herramienta cualquier frase con llaves es la forma más rápida
    # de ejecutar cosas que nadie pidió.
    assert LB.parse_text_tool_call('Mira esto: {"name": "now"} y listo', ["now"]) is None


def test_parse_text_tool_call_acepta_argumentos_ya_en_texto():
    llamada = LB.parse_text_tool_call(
        '{"name": "history", "arguments": "{\\"days\\": 3}"}', ["history"]
    )
    assert llamada is not None
    assert json.loads(llamada["function"]["arguments"]) == {"days": 3}


# ---------------------------------------------------------------------------
# El invariante: `done` una vez y solo una
# ---------------------------------------------------------------------------


def test_done_se_emite_una_sola_vez():
    completion = CompletionFake([[Chunk("hola")]])
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(StoreFake(), MarketFake()),
                          completion=completion, env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    assert len(de_tipo(eventos_, "done")) == 1
    assert eventos_[-1]["type"] == "done"
    assert texto_delta(eventos_) == "hola"


def test_done_tambien_se_emite_sin_rol_configurado():
    completion = CompletionFake([[Chunk("hola")]])
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(StoreFake(roles={})),
                          completion=completion, env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    # El error NO se come el `done`: sin él el cliente espera un cierre que no llega.
    assert len(de_tipo(eventos_, "error")) == 1
    assert len(de_tipo(eventos_, "done")) == 1
    assert completion.n_llamadas == 0


def test_done_se_emite_si_no_hay_claves():
    completion = CompletionFake([[Chunk("hola")]])
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(StoreFake()), completion=completion,
                          env={}, asset_map=MAPA)
    )
    assert len(de_tipo(eventos_, "done")) == 1
    assert completion.n_llamadas == 0
    assert "claves" in de_tipo(eventos_, "error")[0]["content"]


def test_aclose_no_emite_done():
    # Si el cliente desconecta no hay nadie que lea el cierre, y un `yield` dentro de
    # un `finally` revienta con "async generator ignored GeneratorExit".
    async def _run() -> List[str]:
        completion = CompletionFake([[Chunk("hola")]])

        async def _lento() -> AsyncIterator[str]:
            async for c in LB.stream("c1", "general", "hola", deps_de(StoreFake()),
                                     completion=completion, env={"GEMINI_API_KEY": "k"},
                                     asset_map=MAPA):
                yield c

        generador = _lento()
        vistos = []
        async for c in generador:
            vistos.append(c)
            if c.startswith('data: {"type": "delta"'):
                break
        await generador.aclose()
        return vistos

    vistos = asyncio.run(_run())
    assert any('"type": "delta"' in c for c in vistos)
    assert not any('"type": "done"' in c for c in vistos)


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


def test_no_se_cambia_de_proveedor_tras_emitir_texto():
    def _media_y_corta(_kwargs: Any) -> AsyncIterator[Any]:
        async def _gen() -> AsyncIterator[Any]:
            yield Chunk("media respuesta")
            raise Boom("stream cortado")

        return _gen()

    completion = CompletionFake([_media_y_corta])
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(StoreFake()), completion=completion,
                          env={"GEMINI_API_KEY": "k", "GEMINI_API_KEY_2": "k2"}, asset_map=MAPA)
    )
    assert len(completion.calls) == 1, "encadenar otra respuesta deja dos superpuestas"
    assert texto_delta(eventos_) == "media respuesta"
    assert "cortado con texto ya emitido" in de_tipo(eventos_, "error")[0]["content"]
    assert len(de_tipo(eventos_, "done")) == 1


def test_se_cambia_de_clave_si_no_salio_texto():
    completion = CompletionFake([Boom("connection reset"), [Chunk("respuesta del segundo")]])
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(StoreFake()), completion=completion,
                          env={"GEMINI_API_KEY": "k", "GEMINI_API_KEY_2": "k2"}, asset_map=MAPA)
    )
    assert len(completion.calls) == 2
    assert len(set(completion.claves())) == 2, "el segundo intento debe usar la otra clave"
    assert texto_delta(eventos_) == "respuesta del segundo"
    assert de_tipo(eventos_, "done") and not de_tipo(eventos_, "error")


def test_el_429_reintenta_en_la_misma_clave_con_backoff():
    sleep = SleepFake()
    env = {"GEMINI_API_KEY": "k"}
    completion = CompletionFake([Boom("429", status_code=429)])
    tareas = LB.provider_tasks(LB.resolve_model_chain(ROL, {}, env), env)
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(StoreFake(), sleep=sleep),
                          completion=completion, env=env, asset_map=MAPA)
    )
    # Rotar antes de esperar quema las claves de golpe: el 429 dura segundos. Los
    # reintentos son por tarea, y cada tarea tiene su clave.
    assert completion.n_llamadas == LB.MAX_INTENTOS_LIMITE * len(tareas)
    assert len(set(completion.claves()[: LB.MAX_INTENTOS_LIMITE])) == 1
    assert sleep.esperas == [LB.BACKOFF_BASE_S, LB.BACKOFF_BASE_S * 2] * len(tareas)
    assert any("Límite de peticiones" in e["content"] for e in de_tipo(eventos_, "status"))
    assert len(de_tipo(eventos_, "done")) == 1


def test_sin_respuesta_del_modelo_se_persiste_una_vez():
    completion = CompletionFake([[Chunk("")]])
    store = StoreFake()
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(store), completion=completion,
                          env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    # Un turno sin respuesta es amnesia silenciosa; se deja constancia.
    assert [m["content"] for m in store.de("assistant")] == [LB.SIN_RESPUESTA]
    assert len(de_tipo(eventos_, "done")) == 1


# ---------------------------------------------------------------------------
# Bucle de herramientas
# ---------------------------------------------------------------------------


def test_el_tool_call_se_ejecuta_y_se_persiste_el_digest():
    completion = CompletionFake(
        [[Chunk(None, [ToolCall(0, "now", "{}", "call_1")])], [Chunk("No hay entrada limpia.")]]
    )
    store = StoreFake()
    eventos_ = correr(
        lambda: LB.stream("c1", "general", PREGUNTA_COMPLEJA, deps_de(store, MarketFake()),
                          completion=completion, env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    trazas = store.de("tool")
    assert len(trazas) == 1
    assert trazas[0]["name"] == "now"
    assert trazas[0]["tool_call_id"] == "call_1"
    sobre = json.loads(trazas[0]["content"])
    assert sobre["tool"] == "now"
    assert sobre["status"] == "ok"
    assert sobre["round"] == 1
    assert any(e["content"] == "Consultando now" for e in de_tipo(eventos_, "tool"))
    assert texto_delta(eventos_) == "No hay entrada limpia."
    assert len(de_tipo(eventos_, "done")) == 1


def test_la_traza_guarda_el_digest_y_el_modelo_el_payload_completo():
    completion = CompletionFake(
        [[Chunk(None, [ToolCall(0, "account_info", "{}", "call_1")])], [Chunk("Listo.")]]
    )
    store = StoreFake()
    correr(
        lambda: LB.stream("c1", "general", PREGUNTA_COMPLEJA, deps_de(store, MarketFake()),
                          completion=completion, env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    # Al modelo, la respuesta COMPLETA: recortar lo que lee el modelo es inventarse
    # un dato. A la base, el sobre con tamaño, estado y recorte declarado.
    herramienta = [m for m in completion.mensajes_de(1) if m.get("role") == "tool"][0]
    assert herramienta["tool_call_id"] == "call_1"
    assert "balance" in herramienta["content"]
    assert json.loads(store.de("tool")[0]["content"])["status"] == "ok"


def test_una_herramienta_fuera_del_rol_no_se_ejecuta():
    completion = CompletionFake(
        [
            [Chunk(None, [ToolCall(0, "journal_append", '{"entry": "x"}', "call_1")])],
            [Chunk("No puedo escribir en la bitácora.")],
        ]
    )
    store = StoreFake()
    eventos_ = correr(
        lambda: LB.stream("c1", "general", PREGUNTA_COMPLEJA, deps_de(store, MarketFake()),
                          completion=completion, env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    sobre = json.loads(store.de("tool")[0]["content"])
    assert "no permitida" in json.dumps(sobre, ensure_ascii=False)
    assert not store.journal, "una herramienta bloqueada no puede escribir nada"
    assert any(
        "no permitida" in str(m.get("content"))
        for m in completion.mensajes_de(1)
        if m.get("role") == "tool"
    )
    assert len(de_tipo(eventos_, "done")) == 1


def test_el_exceso_de_rondas_avisa_y_no_deja_el_turno_sin_responder():
    guion = [
        [Chunk(None, [ToolCall(0, "now", "{}", "call_{0}".format(i))])] for i in range(8)
    ]
    completion = CompletionFake(guion)
    store = StoreFake()
    eventos_ = correr(
        lambda: LB.stream("c1", "general", PREGUNTA_COMPLEJA, deps_de(store, MarketFake()),
                          completion=completion, env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    assert completion.n_llamadas == LB.DEFAULT_MAX_TOOL_ROUNDS
    assert "Demasiadas rondas" in de_tipo(eventos_, "error")[0]["content"]
    assert [m["content"] for m in store.de("assistant")] == [LB.RONDAS_EXCESO]
    assert len(de_tipo(eventos_, "done")) == 1


def test_un_tool_call_escrito_por_un_local_no_se_muestra_al_usuario():
    escrito = json.dumps({"name": "now", "arguments": {}})
    completion = CompletionFake([[Chunk(escrito)], [Chunk("El reloj va bien.")]])
    store = StoreFake(roles={"general": dict(ROL_LOCAL)})
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(store, MarketFake()),
                          completion=completion, env={}, asset_map=MAPA)
    )
    assert completion.modelos() == ["ollama/llama3.2:3b"] * 2
    # El texto era la llamada, no la respuesta: emitido, el usuario vería un JSON
    # crudo en medio de su respuesta.
    assert texto_delta(eventos_) == "El reloj va bien."
    assert store.de("tool"), "la llamada escrita sí debe ejecutarse"


def test_un_tool_call_escrito_que_no_es_una_llamada_se_entrega_como_texto():
    completion = CompletionFake([[Chunk('{"name": "no_existe", "arguments": {}}')]])
    store = StoreFake(roles={"general": dict(ROL_LOCAL)})
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(store, MarketFake()),
                          completion=completion, env={}, asset_map=MAPA)
    )
    assert not store.de("tool")
    assert texto_delta(eventos_) == '{"name": "no_existe", "arguments": {}}'


# ---------------------------------------------------------------------------
# Historial, persistencia y ruta simple
# ---------------------------------------------------------------------------


def test_el_historial_se_lee_antes_de_guardar_el_turno():
    store = StoreFake(historial=[{"role": "user", "content": "previo"}])
    completion = CompletionFake([[Chunk("hola")]])
    correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(store, MarketFake()),
                          completion=completion, env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    # Al revés, la pregunta llega dos veces: como historial y como mensaje final.
    assert store.orden.index("get_messages") < store.orden.index("add_message:user")
    assert [m["content"] for m in store.de("user")] == ["hola"]
    # Y el historial que se lee es el de ANTES de este turno.
    assert "previo" in completion.texto_de(0, "user")


def test_el_turno_del_modelo_lleva_la_instruccion_de_idioma():
    store = StoreFake()
    completion = CompletionFake([[Chunk("hola")]])
    correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(store), completion=completion,
                          env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    # La instrucción va pegada al mensaje y no al system prompt: es de ESTE turno.
    assert "Responde siempre en español." in completion.texto_de(0, "user")
    assert [m["content"] for m in store.de("user")] == ["hola"]


def test_un_fallo_al_guardar_se_avisa_y_no_se_pierde_la_respuesta():
    completion = CompletionFake([[Chunk("hola")]])
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(StoreRoto()), completion=completion,
                          env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    assert any("No se pudo guardar" in e["content"] for e in de_tipo(eventos_, "status"))
    assert texto_delta(eventos_) == "hola"
    assert len(de_tipo(eventos_, "done")) == 1


def test_la_ruta_simple_solo_consulta_los_topics_mencionados():
    market = MarketFake()
    completion = CompletionFake([[Chunk("Tienes 10.000 de saldo.")]])
    correr(
        lambda: LB.stream("c1", "general", "¿cuál es mi saldo?", deps_de(StoreFake(), market),
                          completion=completion, env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    sistema = completion.texto_de(0, "system")
    assert "Saldo, equity" in sistema
    # "hola, ¿qué tal?" no puede arrastrar las cuatro lecturas en vivo.
    assert "positions" not in market.calls
    assert "history" not in market.calls
    assert "account_info" in market.calls


def test_un_dato_que_no_se_puede_leer_se_dice_con_su_motivo():
    completion = CompletionFake([[Chunk("No lo sé.")]])
    correr(
        lambda: LB.stream("c1", "general", "¿cuál es mi saldo?", deps_de(StoreFake()),
                          completion=completion, env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    sistema = completion.texto_de(0, "system")
    # "MT5 no responde" sería mentira cuando lo que falta es el puerto de mercado.
    assert "NO DISPONIBLE" in sistema
    assert "puerto no cableado" in sistema


def test_un_modelo_local_degrada_a_modo_simple_y_lo_avisa():
    store = StoreFake(roles={"general": dict(ROL_LOCAL)})
    completion = CompletionFake([[Chunk("Resumen sin herramientas.")]])
    eventos_ = correr(
        lambda: LB.stream("c1", "general", PREGUNTA_COMPLEJA, deps_de(store, MarketFake()),
                          completion=completion, env={}, asset_map=MAPA)
    )
    assert any("degradando" in e["content"] for e in de_tipo(eventos_, "status"))
    assert completion.calls[0]["tools"] is None


def test_skip_tools_no_inyecta_datos_ni_ofrece_herramientas():
    completion = CompletionFake([[Chunk("Sin herramientas.")]])
    correr(
        lambda: LB.stream("c1", "general", PREGUNTA_COMPLEJA, deps_de(StoreFake(), MarketFake()),
                          completion=completion, skip_tools=True,
                          env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    assert completion.calls[0]["tools"] is None
    assert "Datos consultados en vivo" not in completion.texto_de(0, "system")


def test_el_reloj_llega_al_modelo_incluso_en_modo_herramientas():
    completion = CompletionFake([[Chunk(None, [ToolCall(0, "now", "{}", "c1")])], [Chunk("vale")]])
    market = MarketFake()
    correr(
        lambda: LB.stream("c1", "general", PREGUNTA_COMPLEJA, deps_de(StoreFake(), market),
                          completion=completion, env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    sistema = completion.texto_de(0, "system")
    # La política de riesgo que lee el modelo habla de killzones en UTC: sin la hora
    # no se puede aplicar.
    assert "UTC" in sistema
    assert completion.calls[0]["tools"]


def test_la_imagen_del_grafico_viaja_con_el_turno():
    completion = CompletionFake([[Chunk("Veo el gráfico.")]])
    correr(
        lambda: LB.stream("c1", "general", "qué ves", deps_de(StoreFake()), completion=completion,
                          chart_image="AAAA", env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    contenido = [m for m in completion.mensajes_de(0) if m["role"] == "user"][0]["content"]
    assert isinstance(contenido, list)
    assert contenido[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_una_url_de_imagen_no_se_parte_por_dos():
    completion = CompletionFake([[Chunk("Veo el gráfico.")]])
    correr(
        lambda: LB.stream("c1", "general", "qué ves", deps_de(StoreFake()), completion=completion,
                          chart_image="https://x/y.png", env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    contenido = [m for m in completion.mensajes_de(0) if m["role"] == "user"][0]["content"]
    assert contenido[1]["image_url"]["url"] == "https://x/y.png"


def test_el_puerto_de_configuracion_ausente_no_tira_el_chat():
    class StoreSinConfig(StoreFake):
        def get_trading_config(self):
            raise RuntimeError("strategy.yaml no existe")

    completion = CompletionFake([[Chunk("Sigo sin config, pero aquí estoy.")]])
    eventos_ = correr(
        lambda: LB.stream("c1", "general", "hola", deps_de(StoreSinConfig()), completion=completion,
                          env={"GEMINI_API_KEY": "k"}, asset_map=MAPA)
    )
    assert not de_tipo(eventos_, "error")
    assert texto_delta(eventos_) == "Sigo sin config, pero aquí estoy."