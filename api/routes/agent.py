"""El agente y su memoria: mensajes en streaming, roles y conversaciones.

Lo que cambia respecto a REF
---------------------------
REF exponía `/api/agent/message` llamando a `ag.stream_agent(...)`, que era la
función del agente legacy, y los tests del agente pasaban por `app`. Aquí el endpoint
llama a `agent.laya_bridge.stream(...)` —el agente de la Fase 5, con puertos— y recibe
los puertos de `app.state.runtime`. El contrato con el cliente (SSE con `data:` y el
tipo dentro del JSON) es el mismo, porque el JS de REF ya lo consume así.

Dos detalles que no son negociables:

1. **`skip_tools` no se expone por defecto.** REF lo tenía como parámetro del body
   y el botón de "responder sin herramientas" lo mandaba. Se mantiene, pero el valor
   por defecto es `false`.
2. **El stream nunca revienta.** `laya_bridge.stream()` ya convierte cualquier
   excepción en un evento `error` y cierra con `done`; el endpoint no añade un
   `try/except` porque un `except` alrededor de un `StreamingResponse` no puede
   cambiar lo que ya se ha enviado.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import deps

router = APIRouter(prefix="/api/agent", tags=["agente"])

#: Cabeceras que hacen que nginx no acumule el stream. Sin ellas, el proxy
#: delivers a lo suyo el evento `done` cuando el chat ya está cerrado.
CABECERAS_SSE = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# -- mensajes -------------------------------------------------------------------


class AgentMessage(BaseModel):
    """Lo que el chat envía. Los nombres son los de REF porque el JS los manda tal cual."""

    message: str
    role_id: str = "general"
    conversation_id: Optional[str] = None
    skip_tools: bool = False
    chart_image: Optional[str] = None


@router.post("/message")
async def agent_message(
    body: AgentMessage,
    deps_agente: Any = Depends(deps.deps_agente),
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> StreamingResponse:
    """El stream del agente en SSE.

    Se crea la conversación si no llega `conversation_id`: el cliente manda el id
    que le devolvió el `stream` anterior, y el primer mensaje de un chat nuevo no
    tiene ninguno. Crearla aquí y no en el cliente evita que el JS tenga que hacer
    un `POST /conversations` extra y que un fallo de ese `POST` deje al usuario sin
    chat con un error invisible.
    """
    from agent import laya_bridge

    mensaje = str(body.message or "").strip()
    if not mensaje:
        raise HTTPException(status_code=400, detail="mensaje vacío")
    conversacion = body.conversation_id
    if not conversacion:
        conversacion = store.create_conversation(role_id=body.role_id).get("id")

    stream = laya_bridge.stream(
        conversation_id=conversacion,
        role_id=body.role_id,
        mensaje=mensaje,
        deps=deps_agente,
        skip_tools=bool(body.skip_tools),
        chart_image=body.chart_image,
    )
    return StreamingResponse(stream, media_type="text/event-stream", headers=CABECERAS_SSE)


# -- roles ----------------------------------------------------------------------


class RoleBody(BaseModel):
    """El rol. `role_id` es opcional porque lo pone la URL.

    Era obligatorio y devolvía 422 a un `PUT` sin él, que es justo lo que manda el
    frontend heredado: el id va en la ruta y el body solo lleva el contenido.
    """

    role_id: str = ""
    name: str = ""
    system_prompt: str = ""
    allowed_tools: List[str] = Field(default_factory=list)
    provider: str = "auto"
    model: str = ""


@router.get("/roles")
def list_roles(store: Any = Depends(deps.store)) -> List[Dict[str, Any]]:
    return store.list_roles(active_only=True)


@router.get("/roles/{role_id}")
def get_role(role_id: str, store: Any = Depends(deps.store)) -> Dict[str, Any]:
    rol = store.get_role(role_id)
    if rol is None:
        raise HTTPException(status_code=404, detail="rol {0} no encontrado".format(role_id))
    return rol


@router.put("/roles/{role_id}")
def put_role(
    role_id: str,
    body: RoleBody,
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Crea o reemplaza un rol. El `role_id` de la URL manda sobre el del body."""
    store.upsert_role(
        role_id,
        body.name or role_id,
        body.system_prompt,
        body.allowed_tools or [],
        provider=body.provider,
        model=body.model,
    )
    return store.get_role(role_id) or {"role_id": role_id}


@router.post("/roles", response_model=None)
def create_role(
    body: RoleBody,
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Crea un rol cuyo id va EN EL BODY.

    Es el alta de la pantalla de roles de REF, donde el campo de texto es el id y no
    hay ruta que lo imponga. Sin `role_id` son 422: un rol con id vacio no se puede
    leer despues, y crearlo sin id devuelve un `200` que luego `GET /roles/{id}`
    devuelve 404.

    Un rol que ya existe se REEMPLAZA, como en el `PUT`, porque la pantalla edita y
    guarda por el mismo camino.
    """
    if not body.role_id:
        raise HTTPException(status_code=422, detail="role_id obligatorio al crear el rol")
    store.upsert_role(
        body.role_id,
        body.name or body.role_id,
        body.system_prompt,
        body.allowed_tools or [],
        provider=body.provider,
        model=body.model,
    )
    return store.get_role(body.role_id) or {"role_id": body.role_id}


@router.delete("/roles/{role_id}")
def delete_role(
    role_id: str,
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Elimina un rol. `404` si no existía.

    `general` no se puede borrar: es el rol al que `laya_bridge` cae cuando el
    pedido no existe, y borrarlo deja el chat sin rol con un `200` y un error
    tres mensajes después.
    """
    if role_id == "general":
        raise HTTPException(status_code=400, detail="'general' no se puede borrar")
    if store.get_role(role_id) is None:
        raise HTTPException(status_code=404, detail="rol {0} no encontrado".format(role_id))
    store.delete_role(role_id)
    return {"ok": True, "role_id": role_id}


# -- configuracion del agente ----------------------------------------------------


class AgentConfigBody(BaseModel):
    """Los ajustes que se pueden cambiar. Todos opcionales: se cambia lo que viene."""

    active_role_id: Optional[str] = None
    fallback_order: Optional[List[str]] = None
    history_limit: Optional[int] = Field(None, ge=1, le=200)
    max_tool_rounds: Optional[int] = Field(None, ge=0, le=20)


def _agent_config(store: Any) -> Dict[str, Any]:
    """El estado de la configuracion, tal y como lo pinta la pantalla de agente.

    `keys` es un booleano por proveedor, NUNCA la clave: es lo que necesita el
    frontend para pintar "falta OPENAI_API_KEY" y no puede ser otra cosa, porque
    esta respuesta la lee cualquiera que abra el panel. Lo que NO se expone es si la
    clave es de verdad valida: eso lo descubre el agente al usarla.

    `available_models` sale de `resolve_model_chain`, el mismo filtro que aplica el
    agente al responder, con los mismos defaults. Duplicar aqui la lista de modelos
    seria una segunda verdad que se desincroniza en cuanto `laya_bridge` cambia una
    alias.
    """
    import os

    from agent import laya_bridge

    settings = store.get_settings()
    claves = {
        "OPENAI_API_KEY": bool(os.getenv("OPENAI_API_KEY")),
        "GEMINI_API_KEY": bool(os.getenv("GEMINI_API_KEY")),
        "ANTHROPIC_API_KEY": bool(os.getenv("ANTHROPIC_API_KEY")),
    }
    return {
        "active_role_id": settings.get("active_role_id", "general"),
        "fallback_order": _lista_json(settings.get("fallback_order")),
        "history_limit": _entero(settings.get("history_limit"), 20),
        "max_tool_rounds": _entero(settings.get("max_tool_rounds"), 6),
        "keys": claves,
        "any_key": any(claves.values()),
        "available_models": laya_bridge.resolve_model_chain(None),
    }


def _lista_json(valor: Any) -> List[str]:
    """El `fallback_order` guardado como texto JSON, o lista vacia si esta roto.

    Un setting corrupto no puede tumbar la pantalla de configuracion: se devuelve la
    lista vacia y el `POST` siguiente lo reescribe. REF hacia `json.loads(...)` a
    pelo y un setting con comilla sin cerrar convertia el panel en un 500.
    """
    if isinstance(valor, list):
        return [str(x) for x in valor]
    try:
        datos = json.loads(valor or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in datos] if isinstance(datos, list) else []


def _entero(valor: Any, por_defecto: int) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return por_defecto


@router.get("/config", response_model=None)
def get_config(store: Any = Depends(deps.store)) -> Dict[str, Any]:
    return _agent_config(store)


@router.post("/config", response_model=None)
def set_config(
    body: AgentConfigBody,
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Cambia lo que venga y devuelve el estado ya actualizado.

    `active_role_id` se ignora en silencio si el rol no existe, en vez de 404: REF
    hacia lo mismo y el frontend guarda el rol activo cada vez que el usuario elige
    uno; un 404 por un rol borrado en otra pestaña no le dice nada util. El rol que
    no exista se caera al general, que es lo que hace `laya_bridge`.
    """
    if body.active_role_id and store.get_role(body.active_role_id):
        store.set_setting("active_role_id", body.active_role_id)
    if body.fallback_order:
        store.set_setting("fallback_order", json.dumps(list(body.fallback_order)))
    if body.history_limit is not None:
        store.set_setting("history_limit", str(int(body.history_limit)))
    if body.max_tool_rounds is not None:
        store.set_setting("max_tool_rounds", str(int(body.max_tool_rounds)))
    return _agent_config(store)


# -- conversaciones ---------------------------------------------------------------


class ConversationBody(BaseModel):
    title: str = "Nueva conversación"
    role_id: str = "general"


@router.get("/conversations")
def list_conversations(store: Any = Depends(deps.store)) -> List[Dict[str, Any]]:
    return store.list_conversations()


@router.post("/conversations")
def create_conversation(
    body: ConversationBody,
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    return store.create_conversation(title=body.title, role_id=body.role_id)


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, store: Any = Depends(deps.store)) -> Dict[str, Any]:
    conversacion = store.get_conversation(conversation_id)
    if conversacion is None:
        raise HTTPException(
            status_code=404, detail="conversación {0} no encontrada".format(conversation_id)
        )
    return conversacion


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    deps_agente: Any = Depends(deps.deps_agente),
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Borra la conversación y sus anclajes de análisis.

    Los anclajes se olvidan con ella a propósito: son coordenadas de un gráfico que
    se pintó en ESTA conversación. Dejarlos vivos permitiría que otro chat validara
    referencias contra un análisis que su usuario nunca vio.
    """
    if store.get_conversation(conversation_id) is None:
        raise HTTPException(
            status_code=404, detail="conversación {0} no encontrada".format(conversation_id)
        )
    store.delete_conversation(conversation_id)
    deps_agente.anchors.forget(conversation_id)
    return {"ok": True, "conversation_id": conversation_id}


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: str,
    limit: int = 20,
    store: Any = Depends(deps.store),
) -> List[Dict[str, Any]]:
    """Los últimos mensajes de la conversación, en orden cronológico.

    `limit` es un máximo de lo que se DEVUELVE, no de lo que se lee: el historial se
    trae entero de la base y se recorta en memoria. Con `limit=200` y una conversación
    larga son 200 filas, y traerlas todas para descartar casi todas es la forma de
    que el chat tarde más cuanto más chat hay.
    """
    return list(store.get_messages(conversation_id, limit=limit))


__all__ = [
    "CABECERAS_SSE",
    "AgentConfigBody",
    "AgentMessage",
    "ConversationBody",
    "RoleBody",
    "router",
]