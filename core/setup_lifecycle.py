"""El ciclo de vida de un setup detectado: nueva, activa, cerrada y su deduplicación.

Modulo PURO, como el resto de `core/`: no lee la base de datos, no habla con el bróker
y no mira el reloj del sistema salvo que se le pase. Recibe la fila de estado anterior y
el veredicto del gate, y devuelve la transición. Quien recibe el `None` no tiene nada
que hacer; quien recibe un nombre tiene que persistirlo.

Por qué vive aquí y no en el servicio del watcher
-------------------------------------------------
En REF esta lógica estaba dentro de `watcher.py`, junto con el escaneo, la
notificación y el envío de órdenes. Mezclada con la I/O era imposible de probar sin
base de datos y sin reloj: los estados posibles del watcher eran once, y once es
suficiente para que todos seijan por lo que hace el resto de la función.

Aquí hay un problema distinto y más pequeño: **cuándo tiene que volver a hablar el
watcher**. Un setup que sigue siendo válido no debe reavisar en cada ciclo (ruido), uno
que ha muerto sí debe cerrarse (para que el siguiente sea una alerta nueva y no un
recargo del anterior), y uno que caducó por TTL tampoco puede quedar abierto para
siempre. Eso es una máquina de estados, y una máquina de estados se prueba con una tabla
de entradas y salidas, no abriendo la base de datos.

Lo que este módulo NO hace
--------------------------
- No decide si un setup es válido. Eso es `core.setup_gate.evaluate_gate`. Aquí solo se
  recibe el `approved` que ya ha salido de ahí, y por eso `gate_ok` es un `bool` y no un
  `dict`: volver a mirar el score sería una segunda aritmética, y dosaritméticas
  divergen.
- No escribe nada. No hay `store` aquí a propósito: una función que decide y otra que
  guarda son dos sitios donde leer qué pasó, y una que hace las dos solo es un sitio
  donde no saber qué pasó.
- No mide la calidad de ejecución ni la compara con nada. Esa puerta es de la
  ejecución, y un aviso de calidad de ejecución en el escaneo se leería como "se puede
  operar" cuando el escaneo no opera nada.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

#: TTL por defecto de un ciclo de alerta, en segundos. 2700 son 45 minutos: el
#: timeframe por defecto del watcher es M15, así que tres velas. Es el valor de REF y
#: lo que declara `watcher.dedup_ttl_sec` en `strategy.yaml`; el default solo se usa
#: cuando la configuración no viene, y por eso se declara en la respuesta del estado en
#: vez de asumirse en silencio.
DEDUP_TTL_DEFECTO = 2700.0

#: Estados que autorizan a abrir un ciclo nuevo. Un setup que estaba en cualquiera de
#: ellos ya terminó, y por lo tanto la fila anterior no debe impedir que el siguiente
#: setup del mismo símbolo se avise como nuevo.
ESTADOS_TERMINADOS = ("", "none", "closed", "expired", "invalidated")

#: Estado de un setup detectado y aún vigente.
ESTADO_ACTIVO = "active"

#: Estado de un setup aprobado que cayó en la ventana de noticias.
ESTADO_NOTICIA = "news_ignored"

#: Estado de un setup que se mandó al bróker.
ESTADO_EJECUTADO = "executed"


def _campo(fila: Any, nombre: str, defecto: Any = None) -> Any:
    """Lee un campo de la fila de estado tanto de dict como de tupla.

    `store.get_setup_state` devuelve un dict, pero el watcher también se puede montar
    sobre una consulta que devuelva tuplas. Admitir las dos formas evita el `AttributeError`
    en el momento en que el setup más antiguo de la tabla —el que nadie recuerda haber
    escrito— se encuentra con un estado que hay que cerrar.
    """
    if fila is None:
        return defecto
    if isinstance(fila, dict):
        return fila.get(nombre, defecto)
    return getattr(fila, nombre, defecto)


def _instante(valor: Any) -> Optional[datetime]:
    """El `created_at` de la fila, como `datetime` con zona, o `None`.

    `None` cuando no hay fecha o no se entiende. No se inventa `epoch`: un estado sin
    fecha no puede estar expirado, y declararlo expirado cerraría un setup vivo por un
    campo de auditoría vacío.
    """
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    if not isinstance(valor, str) or not valor.strip():
        return None
    texto = valor.strip().replace("Z", "+00:00")
    try:
        momento = datetime.fromisoformat(texto)
    except ValueError:
        return None
    return momento if momento.tzinfo else momento.replace(tzinfo=timezone.utc)


def decide_transition(fila_previa: Any,
                      gate_ok: bool,
                      ahora: Optional[datetime] = None,
                      ttl_sec: float = DEDUP_TTL_DEFECTO) -> Optional[str]:
    """La transición de estado para un `{símbolo, timeframe}` dado.

    Devuelve `"new"`, `"closed"` o `None`. `None` significa **no hay nada que hacer**, y
    no "el setup no vale": son dos cosas distintas que en un `dict` de respuesta acabarían
    pareciéndose. Un `None` con un gate que sigue approving es el caso normal —el mismo
    setup de hace treinta segundos, ya avisado— y escribir una fila de estado por él
    renueva el `created_at` y por tanto el TTL, con lo que el setup no caduca nunca.

    El TTL se mide desde el `created_at` de la fila, no desde el último escaneo, y esa es
    toda la razón de que este módulo sea aparte: cambiar la semántica del caducado
    tocando la fila de la base de datos es un cambio que no se ve en ningún diff de la
    ruta que escanea.
    """
    momento = ahora or datetime.now(timezone.utc)
    estado = str(_campo(fila_previa, "status") or "").strip().lower()

    if estado in ESTADOS_TERMINADOS:
        # Sin ciclo abierto solo importa si el gate aprueba AHORA. Se avisa del setup
        # nuevo y no del que dejó de estar en la tabla.
        return "new" if gate_ok else None

    creado = _instante(_campo(fila_previa, "created_at"))
    caducado = bool(
        creado is not None and ttl_sec and ttl_sec > 0
        and (momento - creado).total_seconds() > ttl_sec
    )

    if estado == ESTADO_ACTIVO or estado == ESTADO_NOTICIA:
        # `news_ignored` se deduplica como `active`: ya se registró una vez en la
        # ventana de blackout y reavisar en cada ciclo sería ruido. Se cierra igual
        # que un setup activo, y no revive mientras la señal siga viva: reabrir en
        # mitad de la ventana ya registrada sería la segunda vez que el mismo evento
        # aparece como si fuera nuevo.
        return "closed" if (not gate_ok or caducado) else None

    if estado == ESTADO_EJECUTADO:
        # Ya se mandó una orden: no se manda otra mientras el setup siga vivo. Cierra
        # cuando la señal muere, y ese cierre es lo que permite que el siguiente setup
        # del símbolo sea un ciclo nuevo y no un recargo de este.
        return "closed" if not gate_ok else None

    return None


def clave_de_motivo(motivo: Any) -> str:
    """Firma estable de un motivo de rechazo, sin los números.

    "Score 58.3 < umbral 70.0." y "Score 55.1 < umbral 70.0." son el mismo motivo con
    dos decimales distintos. Si la firma los distinguiera, el mismo rechazo entrando
    otra vez escribiría otra fila, y la tabla de descartes se llenaría de filas que solo
    se diferencian en el número que nadie va a comparar.
    """
    return "".join(c for c in str(motivo or "") if not c.isdigit() and c not in ".-")


def firma_de_rechazo(gate: Optional[Dict[str, Any]]) -> Tuple[Any, ...]:
    """Firma de un rechazo: dirección, banda de score de 5 puntos y motivos.

    La banda y no el score exacto porque un score que se mueve de 58 a 59 es el mismo
    rechazo y no debe generar otra fila; pero un 58 que sube a 62 sí es otro hecho —ha
    dejado de estar tan lejos del umbral— y con una banda de 5 se distingue sin que
    dos rechazos casi idénticos se cuenten como distintos.
    """
    g = gate or {}
    motivos = g.get("reasons") or []
    try:
        banda = int(float(g.get("score") or 0.0) // 5)
    except (TypeError, ValueError):
        banda = 0
    return (g.get("direction"), banda, tuple(sorted(clave_de_motivo(m) for m in motivos)))


#: La dirección del gate y la clave del `risk_engine` del snapshot no se llaman igual.
#: `evaluate_gate` elige entre `bull` y `bear`; aquí, sin este mapa, `direccion="BUY"`
#: buscaría `"buy"` en el snapshot, no lo encontraría y devolvería un breakdown vacío:
#: una fila de auditoría con `{}` en lugar del desglose, sin ningún error que lo diga.
LADO_EN_EL_SNAPSHOT = {"BUY": "bull", "SELL": "bear"}


def componentes_del_score(snapshot: Optional[Dict[str, Any]],
                          direccion: Optional[str]) -> Dict[str, Any]:
    """Los componentes del score del lado elegido, para la fila de auditoría.

    Viene del `risk_engine` del snapshot tal cual. Recomponerlos aquí significaría
    volver a sumar los pesos, y una fila de auditoría cuya suma no da el score que dice
    haber recibido es peor que una fila sin desglose: se puede comprobar la segunda, no
    la primera.

    `direccion` es la del gate (`"BUY"`/`"SELL"`) y la clave del snapshot es
    `bull`/`bear`; el mapa es lo que traduce una en otra. La comparación no distingue
    mayúsculas porque la dirección viene de una etiqueta de la UI con más de una fuente.

    Devuelve una copia **profunda**. La salida va al `breakdown_json` de una fila de
    auditoría, y un `dict()` a secas dejaría los dicts anidados compartida con el
    snapshot: bastaría un `componentes["killzone"]["in_killzone"] = ...` en el camino que
    serializa para que la fila registrara un killzone que el motor nunca dijo. Es una
    copia pequeña de un puñado de claves y evita tener que confiar en que nadie muta.
    """
    lado = LADO_EN_EL_SNAPSHOT.get(str(direccion or "").upper())
    if lado is None:
        return {}
    re_ = (snapshot or {}).get("risk_engine") or {}
    entrada = re_.get(lado) or {}
    comps = entrada.get("components") or {} if isinstance(entrada, dict) else {}
    return deepcopy(comps) if isinstance(comps, dict) else {}


__all__ = [
    "DEDUP_TTL_DEFECTO",
    "ESTADO_ACTIVO",
    "ESTADO_EJECUTADO",
    "ESTADO_NOTICIA",
    "ESTADOS_TERMINADOS",
    "LADO_EN_EL_SNAPSHOT",
    "clave_de_motivo",
    "componentes_del_score",
    "decide_transition",
    "firma_de_rechazo",
]
