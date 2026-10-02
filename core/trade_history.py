"""Consolidación del historial de deals: deals → operaciones.

Vive fuera de la API porque es DOMINIO, no infraestructura: decide qué deal manda
para la dirección de una operación, y esa decisión estuvo equivocada. El bug era
que la web tomaba el último deal (el de cierre, cuyo tipo es el opuesto al de la
posición: un BUY se cierra con un deal SELL) y por eso mostraba la dirección
invertida y perdía el precio de apertura. tests/test_trade_history.py lo cubre.

La función es pura a propósito: recibe deals, devuelve dicts. No llama a ningún
broker, no toca la base de datos y no importa API ni store. Eso la hace testeable
sin terminal, que es lo que permite que el bug de la dirección invertida se
detectara antes de llegar a producción.

Los deals llegan como objetos con atributos (o algo que los soporte vía
`getattr`). Los adaptadores los convierten desde el proveedor; el núcleo solo
necesita los campos que se leen aquí: `position_id`, `entry`, `type`, `time`,
`profit`, `commission`, `swap`.

MetaTrader5 representa entry/type con enteros. Los literales de aquí son los
mismos que usa la terminal (DEAL_ENTRY_IN=0, DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1).
Se comparan por valor y NO se importan de MetaTrader5 a propósito: importar el
módulo real haría que el test necesitara la librería instalada y, con ella, que un
cambio de literales en MT5 rompiera la suite sin que nadie lo haya decidido.
"""

from __future__ import annotations

# Valores literales de MetaTrader5. Deliberadamente NO importados: ver el docstring.
DEAL_ENTRY_IN = 0
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1


def consolidate_deals(deals):
    """Agrupa deals por position_id en operaciones colapsadas.

    Devuelve una operación por posición con la dirección y precio reales de
    ENTRADA (deal DEAL_ENTRY_IN) y el precio/hora de CIERRE (último deal del
    histórico), sumando profit/comisión/swap de todas las patas (parciales,
    swap diario, etc.).

    Reglas que importan, y que no son obvias:

    - `position_id == 0` se descarta. MT5 lo usa en deals que no pertenecen a
      ninguna posición (fundos, comisiones sueltas) y agruparlos bajo 0 mezcla
      operaciones distintas.
    - La DIRECCIÓN sale del deal de ENTRADA, nunca del de cierre: el de cierre
      tiene el tipo opuesto, y tomarlo invierte la operación en pantalla.
    - Entre varios deals de entrada gana el más ANTIGUO por timestamp, no el
      primero que aparece en la lista: el orden de `history_deals_get` no está
      garantizado.
    - Entre varios deals de salida gana el más RECIENTE, que es el que tiene el
      precio y la hora de cierre reales.
    """
    by_position = {}
    for d in deals:
        pid = getattr(d, "position_id", 0)
        if pid == 0:
            continue
        rec = by_position.setdefault(pid, {
            "entry": None, "exit": None,
            "profit": 0.0, "commission": 0.0, "swap": 0.0,
        })
        rec["profit"] += getattr(d, "profit", 0.0) or 0.0
        rec["commission"] += getattr(d, "commission", 0.0) or 0.0
        rec["swap"] += getattr(d, "swap", 0.0) or 0.0
        if getattr(d, "entry", None) == DEAL_ENTRY_IN:
            if rec["entry"] is None or getattr(d, "time", 0) < rec["entry"].time:
                rec["entry"] = d
        else:
            if rec["exit"] is None or getattr(d, "time", 0) >= rec["exit"].time:
                rec["exit"] = d
    return by_position


def direction_of(rec):
    """Dirección de una operación consolidada, o None si no se puede determinar.

    Se decide por el deal de ENTRADA. Si solo existe el de cierre, se infiere por
    su tipo invertido, que es el mismo criterio con el que MT5 cierra una posición:
    una posición BUY se cierra con un deal SELL.

    Si no hay ninguno de los dos, devuelve None en vez de adivinar: una dirección
    inventada en el historial es peor que un hueco, porque ordena y filtra.
    """
    entry, exit_ = rec.get("entry"), rec.get("exit")
    if entry is not None:
        return "BUY" if entry.type == DEAL_TYPE_BUY else "SELL"
    if exit_ is not None:
        # El deal de cierre lleva el tipo contrario al de la posición.
        return "BUY" if exit_.type == DEAL_TYPE_SELL else "SELL"
    return None