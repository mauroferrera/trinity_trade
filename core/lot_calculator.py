"""Normalización de tamaño de posición entre mercados.

Existe porque "cuántos lotes" no es la misma pregunta en los tres mercados. En
forex un lote son 100.000 unidades de la divisa base; en B3 un minicontrato vale
un punto del índice; en cripto perpetuo el tamaño se expresa en nocional (USDT) y
no en contratos. Aplicar el cálculo de un mercado a otro no falla: produce un
tamaño plausible que nadie nota hasta que la posición es 100x o 0.01x la
querida. Este módulo hace explícita esa diferencia.

Está separado de `risk_engine` a propósito. El risk engine decide CUÁNTO arriesgar
(en %, sobre el equity); este módulo decide CÓMO se expresa esa cantidad en la
unidad que el broker de ese mercado entiende. Mezclarlos obligaba a que cada
símbolo nuevo tocara el motor de riesgo.

Cada calculador es una FUNCIÓN PURA: recibe specs declaradas y devuelve el tamaño
redondeado según los pasos del instrumento. No consulta el broker ni lee ficheros:
los specs llegan desde el adaptador o desde `config/asset_sources_map.yaml`.

Redondeo: siempre hacia ABAJO (`math.floor` sobre el valor en pasos). Un redondeo
hacia arriba puede devolver un tamaño que excede el riesgo autorizado, que es
justo el error que este módulo no debe poder cometer. El mínimo del broker manda
igual: si el cálculo da menos que un lote, se devuelve un lote, y eso se reporta
explícitamente en `clamped_to_min` para que quien llama no lo confunda con un
tamañooptimal.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

__all__ = [
    "LotSpec",
    "MIN_LOT",
    "standard_forex_lots",
    "risk_per_unit",
    "b3_mini_contracts",
    "b3_bonds",
    "crypto_notional_usdt",
    "CALCULATORS",
    "calculate_size",
]

# Menor incremento negociable que se considera en cualquier mercado. Es el suelo
# de MT5 para forex y también el de los minicontratos B3, así que sirve de
# mínimo global cuando el spec no declara otro.
MIN_LOT = 1.0


class LotSpec:
    """Specs mínimas del instrumento para convertir riesgo a tamaño.

    Se construye desde un dict (el `execution` de strategy.yaml o el bloque del
    símbolo en `asset_sources_map.yaml`) para no obligar a los llamadores a
    conocer las palabras clave exactas del YAML.

    lot_size      unidades de la divisa base por lote (forex) o por contrato.
    tick_value    valor en divisa de la cuenta de un tick del tamaño mínimo.
    tick_size     salto de precio mínimo, en la unidad del símbolo.
    min_lot       incremento mínimo negociable (default MIN_LOT).
    lot_step      incremento entre tamaños válidos (default min_lot).
    """

    __slots__ = ("lot_size", "tick_value", "tick_size", "min_lot", "lot_step")

    def __init__(self, lot_size: float, tick_value: float, tick_size: float,
                 min_lot: float = MIN_LOT, lot_step: Optional[float] = None):
        if lot_size <= 0:
            raise ValueError(f"lot_size debe ser > 0, recibido {lot_size}")
        if tick_size <= 0:
            raise ValueError(f"tick_size debe ser > 0, recibido {tick_size}")
        if min_lot <= 0:
            raise ValueError(f"min_lot debe ser > 0, recibido {min_lot}")
        self.lot_size = float(lot_size)
        self.tick_value = float(tick_value)
        self.tick_size = float(tick_size)
        self.min_lot = float(min_lot)
        self.lot_step = float(lot_step if lot_step else min_lot)

    @classmethod
    def from_dict(cls, spec: Dict[str, Any]) -> "LotSpec":
        """Construye el spec desde un dict de configuración.

        Acepta alias (`lot_size`/`contract_size`, `tick_value`/`value_per_tick`)
        porque `strategy.yaml` y los specs de broker no usan los mismos nombres, y
        no merece la pena obligar a traducir antes de calcular.
        """
        def pick(*names):
            for n in names:
                if spec.get(n) is not None:
                    return spec[n]
            raise ValueError(f"spec sin ninguno de {names}: {sorted(spec)}")

        return cls(
            lot_size=pick("lot_size", "contract_size"),
            tick_value=pick("tick_value", "value_per_tick"),
            tick_size=pick("tick_size", "point"),
            min_lot=spec.get("min_lot") or MIN_LOT,
            lot_step=spec.get("lot_step") or spec.get("min_lot") or MIN_LOT,
        )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return (f"LotSpec(lot_size={self.lot_size}, tick_value={self.tick_value}, "
                f"tick_size={self.tick_size}, min_lot={self.min_lot}, "
                f"lot_step={self.lot_step})")


def _round_down(value: float, step: float) -> float:
    """Redondea `value` a múltiplos de `step`, siempre hacia abajo."""
    if step <= 0:
        return value
    return math.floor(value / step) * step


def _finish(size: float, spec: LotSpec, risk_per_unit: float) -> Dict[str, Any]:
    """Aplica mínimo y redondeo final, y reporta si hubo que subir al mínimo.

    `risk_per_unit` es el riesgo en divisa de la cuenta que arriesga UNA unidad
    (un lote, un contrato) al llegar al SL. Se necesita para reportar `risk_at_sl`
    con sentido: multiplicar solo por `tick_value` daría el valor de un tick, no el
    riesgo de la posición, y esa cifra es la que se compara contra el presupuesto.
    """
    stepped = _round_down(size, spec.lot_step)
    clamped = stepped < spec.min_lot
    if clamped:
        stepped = spec.min_lot
    # Sin decimales falsos: un tamaño que se imprime como "0.30000000000004"
    # acaba en el log y alguien lo copia a una orden.
    return {
        "lots": round(stepped, 8),
        "clamped_to_min": clamped,
        "risk_per_unit": round(risk_per_unit, 8),
        "risk_at_sl": round(stepped * risk_per_unit, 8),
    }


def _risk_per_unit(sl_distance: float, spec: LotSpec, unidad: str) -> float:
    """Riesgo en divisa de la cuenta que arriesga una unidad al llegar al SL.

    Compartido por los cuatro cálculos porque TODOS responden a la misma
    pregunta: cuántos ticks recorre el precio hasta el stop, y cuánto vale cada
    tick en dinero. Lo que cambia entre mercados es el spec (la unidad del
    `tick_value`), no la fórmula.
    """
    if sl_distance <= 0:
        raise ValueError(f"sl_distance debe ser > 0, recibido {sl_distance}")
    sl_ticks = sl_distance / spec.tick_size
    # Un SL más fino que un tick no es un stop barato: es un stop que el broker no
    # puede colocar. Sin esta guarda el cálculo sale "barato" (sl_ticks < 1) y
    # devuelve un tamaño desproporcionado que parece válido.
    if sl_ticks < 1.0:
        raise ValueError(
            f"sl_distance={sl_distance} es menor que un tick ({spec.tick_size}) en "
            f"{sl_ticks:.4f} ticks. Un stop más fino que el tick no es colocable: "
            "revisa sl_distance contra el tick_size del símbolo."
        )
    risk = sl_ticks * spec.tick_value
    if risk <= 0:
        raise ValueError(
            f"riesgo por {unidad} nulo (sl_ticks={sl_ticks}, tick_value={spec.tick_value}). "
            "Un SL menor que el tick no significa riesgo cero: significa que el "
            "stop no es colocable en ese instrumento. Revisa sl_distance y tick_size."
        )
    return risk


def risk_per_unit(sl_distance: float, spec: LotSpec) -> float:
    """Riesgo en divisa de la cuenta de UNA unidad al llegar al SL. Forex: un lote.

    Existe con nombre público porque la pregunta la hacen dos sitios y tienen que
    darexactamente la misma cifra: el que DIMENSIONA el lote y el que después
    COMPRUEBA que ese lote cabe en el presupuesto de riesgo. Si los dos calcularan
    el riesgo por su cuenta, una diferencia de un factor entre ambos no daría
    ningún error: el lote se dimensionaría con una cuenta y se validaría con
    otra, y ambos números saldrían redondos y con aspecto de razonables.

    **El `tick_value` NO se multiplica por el `lot_size`.** En MT5,
    `SYMBOL_TRADE_TICK_VALUE` ya es el dinero que mueve la cuenta por un tick de
    UN lote: el tamaño del contrato ya está dentro. Multiplicarlo otra vez por
    100.000 en EURUSD infla el riesgo del lote en cinco órdenes de magnitud, y el
    síntoma es que toda operación se rechaza por "riesgo excede el presupuesto"
    con cifras de millones cuando el presupuesto son decenas. El `lot_size` sigue
    declarándose porque `LotSpec` lo valida y porque un spec sin contrato
    declarado no es un spec de forex.
    """
    return _risk_per_unit(sl_distance, spec, "lote")


def standard_forex_lots(risk_amount: float, sl_distance: float, spec: LotSpec) -> Dict[str, Any]:
    """Forex estándar: 1 lote = 100.000 unidades de la divisa base.

    El riesgo por lote sale de la distancia al SL, que es el múltiplo del
    `tick_size` que el precio recorre hasta el stop.

    `sl_distance` va en UNIDADES DE PRECIO del símbolo (12 pips = 0.0012 en
    EURUSD), no en pips: mezclar las dos es el error clásico que produce un
    tamaño 100x mayor o menor.
    """
    risk_per_lot = _risk_per_unit(sl_distance, spec, "lote")
    return _finish(risk_amount / risk_per_lot, spec, risk_per_lot)


def b3_mini_contracts(risk_amount: float, sl_distance: float, spec: LotSpec) -> Dict[str, Any]:
    """B3 minicontratos: 1 contrato = 1 punto del índice.

    El cálculo es idéntico al forex una vez expresado el tamaño en contratos, pero
    se mantiene como función propia porque los specs no son los mismos: el punto
    de un minicontrato de índice vale el valor del punto por UN contrato, no por
    100.000 unidades. Mezclar ambas unidades da tamaños sin sentido.
    """
    risk_per_contract = _risk_per_unit(sl_distance, spec, "contrato")
    return _finish(risk_amount / risk_per_contract, spec, risk_per_contract)


def b3_bonds(risk_amount: float, sl_distance: float, spec: LotSpec) -> Dict[str, Any]:
    """B3 Treasuries (NTN-F): el tick es fractionado.

    Los Bonds cotizan en EIN con 6 decimales y el salto mínimo es 0.000005 frente
    a un precio de ~1400, así que un tick de precio NO equivale a un tick de
   contratos del instrumento: por eso la distancia se expresa en ticks de PRECIO y el
    valor en divisa vive en `tick_value`, igual que en los otros calculadores. Lo
    que cambia aquí es que el redondeo del tamaño debe ser al alza en el tick del
    contrato (el Bonds no acepta fracciones), y eso lo describe el `lot_step` del
    spec, no esta función.
    """
    return b3_mini_contracts(risk_amount, sl_distance, spec)


def crypto_notional_usdt(risk_amount: float, sl_distance: float, spec: LotSpec,
                         entry_price: Optional[float] = None) -> Dict[str, Any]:
    """Cripto perpetuo: el tamaño se expresa en UNIDADES DE LA BASE.

    En un perpetuo no se compran "lotes": se compran BTC/ETH, y el nocional
    (unidades x precio) es lo que expone la posición. Aquí el resultado se devuelve
    como notional en USDT porque es la unidad en la que estos exchanges expresan
    el tamaño (`qty` en algunos, `notional` en otros).

    `entry_price` es necesario: sin él no hay forma de pasar de unidades a
    nocional. Si falta, se devuelve el cálculo en unidades junto con la razón, en
    vez de un notional inventado.
    """
    if entry_price is not None and entry_price <= 0:
        raise ValueError(f"entry_price debe ser > 0, recibido {entry_price}")
    risk_per_unit = _risk_per_unit(sl_distance, spec, "unidad")
    units = _finish(risk_amount / risk_per_unit, spec, risk_per_unit)
    if entry_price is None:
        units["notional_usdt"] = None
        units["notional_error"] = "entry_price requerido para expresar el nocional"
        return units
    units["notional_usdt"] = round(units["lots"] * float(entry_price), 8)
    units.pop("notional_error", None)
    return units


# Registro de los calculadores por `lot_calculator` de config/asset_sources_map.yaml.
# Que el YAML nombre un calculador que no existe debe fallar al cargar el mapa, no
# cuando ya haya una operación en vuelo.
CALCULATORS = {
    "standard_forex_lots": standard_forex_lots,
    "b3_mini_contracts": b3_mini_contracts,
    "b3_bonds": b3_bonds,
    "crypto_notional_usdt": crypto_notional_usdt,
}


def calculate_size(calculator: str, risk_amount: float, sl_distance: float,
                   spec: Any, **kwargs: Any) -> Dict[str, Any]:
    """Despacha al calculador nombrado por `calculator`.

    `spec` acepta un `LotSpec` o un dict (se convierte aquí). Los `kwargs` extra
    son para los calculadores que necesiten más (hoy solo `crypto_notional_usdt`
    con `entry_price`).
    """
    fn = CALCULATORS.get(calculator)
    if fn is None:
        raise KeyError(
            f"calculador de lotes desconocido: {calculator!r}. "
            f"Disponibles: {sorted(CALCULATORS)}"
        )
    if not isinstance(spec, LotSpec):
        spec = LotSpec.from_dict(spec)
    return fn(risk_amount, sl_distance, spec, **kwargs)