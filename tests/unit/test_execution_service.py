"""La puerta ÚNICA de salida: cada puerta tiene su test de rechazo.

Este módulo es donde se decide si una orden sale. Un "no se manda" sin test es
una intención, y las intenciones se degradan solas la primera vez que hay prisa:
alguien añade un `return` nuevo, nadie lo cubre porque los tests verdes ya estaban,
y el filtro que nadie iba a tocar se queda sin comprobar.

Por eso la estructura de cada test es la misma: se monta un servicio con UNA cosa
rota, se llama a `execute_market_trade`, y se mira el `status` de la respuesta. El
`status` es el contrato con el cliente HTTP —503 es reintentable, 403 es "no lo
intentes otra vez"— y por eso se comprueba, no solo el `ok`. Un filtro que
devolviera 200 con `ok: false` dejaría al cliente sin forma de distinguir.

**Por qué no se reusa `FakeMT5`.** Aquí no se prueba el envío: eso es
`test_mt5_execution.py`. Lo que se prueba es qué llega al puerto y, sobre todo,
qué NO llega. Un doble de bróker completo haría que los tests de puerta tuvieran
que fabricar cotizaciones y specs para poder comprobar un filtro de lista blanca,
y un test que necesita cinco cosas falsas para estar en pie prueba menos de lo que
parece. Aquí los dobles son del tamaño de la puerta que se prueba.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from api.services import execution as execution_mod
from api.services.execution import (
    VERDICT_IGNORED_NEWS,
    ExecutionService,
    _status_de_ejecucion,
    execution_from,
)
from adapters.forex import mt5_execution
from core import execution_quality

# ---------------------------------------------------------------------------
# Dobles mínimos
# ---------------------------------------------------------------------------


class SpecFalso:
    """Lo que `_lote_por_riesgo` y `execution_quality` leen del spec.

    Los números son de EURUSD en 5 decimales con un contrato de 100.000: con un
    tick size de 0.00001 y un tick value de 1.0, un lote de 0.10 pierde 100 por
    pip, que es la cuenta que hace un humano al mirar la pantalla de riesgo.
    """

    symbol = "EURUSD"
    point = 0.00001
    digits = 5
    trade_contract_size = 100000.0
    trade_tick_size = 0.00001
    trade_tick_value = 1.0
    contract_size = 100000.0
    tick_size = 0.00001
    tick_value = 1.0
    volume_min = 0.01
    volume_max = 100.0
    volume_step = 0.01

    def __getattr__(self, nombre: str) -> Any:
        """Todo lo que no se declara es 0.0, como un spec sin ese campo.

        `getattr(spec, "x", 0.0)` es el patrón que usa el servicio; si el doble no
        tiene el atributo, ese `getattr` devuelve 0.0 y el test pasa por el mismo
        camino que un spec real sin el campo. Un doble que respondiera a todo
        inventaría capacidades que el bróker no ha dicho que tiene.
        """
        return 0.0


class MarketFalso:
    """Puente a mercado: spec, precio y cuenta. Nada más.

    Cada método deja constancia en `llamadas` para poder afirmar que una puerta se
    cortocircuitó ANTES de tocar el bróker. Un test que solo comprueba el status
    no distingue "el filtro funcionó" de "el filtro no llegó a evaluarse porque
    algo antes falló".
    """

    def __init__(self, spec: Any = "ok", precio: Any = "ok", balance: Any = 10000.0,
                 riesgo: Any = None) -> None:
        self._spec = spec
        self._precio = precio
        self._balance = balance
        self._riesgo = {"blocked": False, "reasons": []} if riesgo is None else riesgo
        self.llamadas: List[str] = []

    def spec(self, simbolo: str) -> Any:
        self.llamadas.append("spec")
        if isinstance(self._spec, Exception):
            raise self._spec
        return SpecFalso() if self._spec == "ok" else self._spec

    def price(self, simbolo: str) -> Any:
        self.llamadas.append("price")
        if isinstance(self._precio, Exception):
            raise self._precio
        if self._precio == "ok":
            return {"symbol": simbolo, "bid": 1.10000, "ask": 1.10012, "time": 1_700_000_000}
        return self._precio

    def account_info(self) -> Dict[str, Any]:
        self.llamadas.append("account_info")
        return {"balance": self._balance, "equity": self._balance, "currency": "USD"}

    def daily_risk_state(self) -> Dict[str, Any]:
        self.llamadas.append("daily_risk_state")
        if isinstance(self._riesgo, Exception):
            raise self._riesgo
        return self._riesgo


class StoreFalso:
    """Auditoría: `get_trading_config`, `log_setup`, `update_trade_result`."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None,
                 log_setup_raises: bool = False) -> None:
        self._cfg = cfg if cfg is not None else CFG_MINIMA
        self._log_setup_raises = log_setup_raises
        self.filas: List[Dict[str, Any]] = []
        self.resultados: List[Dict[str, Any]] = []
        self._siguiente_id = 1000

    def get_trading_config(self) -> Dict[str, Any]:
        return dict(self._cfg)

    def log_setup(self, entry: Dict[str, Any]) -> int:
        if self._log_setup_raises:
            raise RuntimeError("disco lleno")
        self.filas.append(dict(entry))
        self._siguiente_id += 1
        return self._siguiente_id

    def update_trade_result(self, setup_id: int, resultado: Dict[str, Any]) -> None:
        self.resultados.append({"setup_id": setup_id, **dict(resultado)})


class NewsFalso:
    """El calendario. `gate()` devuelve el veredicto que se le pase, o revienta."""

    def __init__(self, veredicto: Any = None) -> None:
        self._veredicto = {} if veredicto is None else veredicto
        self.llamadas = 0

    def gate(self) -> Dict[str, Any]:
        self.llamadas += 1
        if isinstance(self._veredicto, Exception):
            raise self._veredicto
        return dict(self._veredicto)


class PuertoFalso:
    """El puerto de ejecución. Recuerda lo que le pidieron, sin bróker detrás."""

    def __init__(self, resultado: Optional[Dict[str, Any]] = None,
                 cierre: Optional[Dict[str, Any]] = None,
                 riesgo_por_lote: Any = 100.0) -> None:
        self.resultado = resultado if resultado is not None else {
            "ok": True, "retcode": 10009, "status": "llenada", "fill_price": 1.10012,
            "deal": 555, "order": 666, "filling": mt5_execution.ORDER_FILLING_FOK,
            "volume": 0.10, "attempts": [],
        }
        self.cierre = cierre
        self.riesgo_por_lote = riesgo_por_lote
        self.envios: List[Dict[str, Any]] = []
        self.cierres: List[Dict[str, Any]] = []
        self.preguntas_riesgo: List[Any] = []

    def profit_per_lot(self, simbolo: str, lote: float, entrada: float, sl: float) -> Any:
        self.preguntas_riesgo.append((simbolo, lote, entrada, sl))
        if isinstance(self.riesgo_por_lote, Exception):
            raise self.riesgo_por_lote
        return self.riesgo_por_lote

    def send_market_order(self, simbolo: str, accion: str, lote: float, **kw: Any) -> Dict[str, Any]:
        self.envios.append({"symbol": simbolo, "action": accion, "volume": lote, **kw})
        return dict(self.resultado)

    def close_position(self, **kw: Any) -> Dict[str, Any]:
        self.cierres.append(dict(kw))
        if self.cierre is None:
            return {"ok": True, "retcode": 10009, "status": "cerrada",
                    "ticket": kw.get("ticket"), "filling": 0}
        if isinstance(self.cierre, Exception):
            raise self.cierre
        return dict(self.cierre)


#: Configuración mínima que deja pasar un setup decente: EURUSD en la lista
#: blanca, R:R 2, TTL amplia, 0.5% de riesgo y las distancias de SL declaradas.
CFG_MINIMA: Dict[str, Any] = {
    "symbols_allow": ["EURUSD"],
    "min_rr": 2.0,
    "setup_ttl_minutes": 40.0,
    "sl_distance": 0.00100,
    "magic": 8882026,
    "comment": "ILOF Exec",
}

#: Entrada válida: BUY a 1.10012 con SL a 50 pips y TP al doble.
ENTRADA_BUY = 1.10012
SL_BUY = 1.09912
TP_BUY = 1.10212
INVALIDACION_BUY = 1.09850


def _servicio(market: Any = None, store: Any = None, news: Any = None,
              puerto: Any = "auto") -> ExecutionService:
    """Servicio con un puerto que FUNCIONA salvo que se pida otra cosa.

    El defecto sería que `_servicio()` montara una instalación sin puerto: entonces
    casi todos los tests de puerta terminarían en `NO_EXECUTION_PORT` y no probarían
    la puerta que creían estar probando. `puerto=None` explícito sigue siendo
    `None` — así se prueba el caso de solo lectura.
    """
    return ExecutionService(
        market=market if market is not None else MarketFalso(),
        store=store if store is not None else StoreFalso(),
        news=news,
        execution=PuertoFalso() if puerto == "auto" else puerto,
    )


def _ejecuta(svc: ExecutionService, **kw: Any):
    """Atajo para una operación que debería salir, con lo que la ruta mandaría."""
    params: Dict[str, Any] = {
        "symbol": "EURUSD",
        "action": "BUY",
        "volume": 0.10,
        "sl_distance": 0.00100,
        "tp_distance": 0.00200,
        "score": 72.0,
        "verdict": "APROBADO",
        "invalidate_level": INVALIDACION_BUY,
        "planned_entry": ENTRADA_BUY,
        "source": "unit",
    }
    params.update(kw)
    return svc.execute_market_trade(**params)


# ---------------------------------------------------------------------------
# 0. Petición bien formada: ni esto toca el bróker
# ---------------------------------------------------------------------------


def test_sin_simbolo_no_toca_nada():
    svc = _servicio()
    cuerpo, status = svc.execute_market_trade(symbol="  ", action="BUY")
    assert status == 400
    assert "símbolo" in cuerpo["error"].lower()


def test_accion_que_no_es_buy_ni_sell_se_rechaza():
    svc = _servicio()
    cuerpo, status = svc.execute_market_trade(symbol="EURUSD", action="CERRAR")
    assert status == 400
    assert "BUY o SELL" in cuerpo["error"]
    # Ni la lista blanca ni el bróker: la petición no está formada.
    assert svc._market.llamadas == []


def test_la_accion_se_normaliza_antes_de_mirar_las_puertas():
    """Un " buy " con espacios es una orden válida; "B U Y" no.

    La normalización tiene que pasar ANTES de comparar contra BUY/SELL y contra
    `symbols_allow`, porque las dos listas están en mayúsculas. Si se normalizara
    después, un " buy " rechazado por acción inválida sería el mismo fallo que un
    "buy" que se cuela sin comprobar la lista blanca.
    """
    svc = _servicio()
    cuerpo, status = _ejecuta(svc, action=" buy ")
    assert status == 200, cuerpo
    assert cuerpo["action"] == "BUY"

    svc2 = _servicio()
    _, status2 = _ejecuta(svc2, action="B U Y")
    assert status2 == 400


# ---------------------------------------------------------------------------
# 1. Lista blanca
# ---------------------------------------------------------------------------


def test_simbolo_fuera_de_symbols_allow_es_403():
    """403, no 400: reintentarlo sin cambiar la config es inútil."""
    svc = _servicio()
    cuerpo, status = svc.execute_market_trade(symbol="DE40", action="BUY", volume=0.1,
                                              sl_distance=0.001, tp_distance=0.002)
    assert status == 403
    assert cuerpo["status"] == "SYMBOL_NOT_ALLOWED"
    assert "DE40" in cuerpo["error"]
    # Y no se llegó a leer la cuenta: la lista blanca va antes.
    assert "daily_risk_state" not in svc._market.llamadas
    assert "spec" not in svc._market.llamadas


def test_la_lista_blanca_acepta_una_cadena_con_separadores():
    """`symbols_allow` también llega como texto, no solo como lista de YAML."""
    store = StoreFalso({"symbols_allow": "eurusd, gbpusd"})
    svc = _servicio(store=store)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo


def test_una_lista_blanca_vacia_permite_todo_y_lo_dice():
    """El caso peligroso: sin lista blanca, no hay lista blanca.

    Bloquear todo dejaría una instalación recién desplegada sin poder hacer nada
    ni probar que el envío funciona. Se permite, pero se DECLARA: una lista
    vacía que no avisa es indistinguishable de una lista que sí protege.
    """
    store = StoreFalso({"symbols_allow": []})
    svc = _servicio(store=store)
    cuerpo, status = _ejecuta(svc, symbol="DE40")
    assert status == 200, cuerpo
    assert any("symbols_allow" in w for w in cuerpo["warnings"])
    # Y quien sí bloquea lo dice en el mensaje de error:
    svc2 = _servicio(store=StoreFalso({"symbols_allow": ["EURUSD"]}))
    cuerpo2, status2 = svc2.execute_market_trade(symbol="DE40", action="BUY")
    assert status2 == 403
    assert "EURUSD" in cuerpo2["error"]


# ---------------------------------------------------------------------------
# 2. Riesgo del día
# ---------------------------------------------------------------------------


def test_riesgo_bloqueado_por_los_topes_del_dia():
    market = MarketFalso(riesgo={"blocked": True,
                                 "reasons": ["4 trades hoy >= 3", "pérdida diaria 2.1% >= 2%"]})
    svc = _servicio(market=market)
    cuerpo, status = _ejecuta(svc)
    assert status == 403
    assert cuerpo["status"] == "BLOCKED_BY_RISK"
    # Los motivos del bróker se enseñan enteros, no resumidos: quien lee el 403
    # tiene que saber cuál de los dos topes lo paró.
    assert "4 trades hoy" in cuerpo["error"]
    assert "pérdida diaria" in cuerpo["error"]
    assert "spec" not in market.llamadas


def test_no_se_poder_leer_el_riesgo_es_503_no_permiso():
    """Un fallo de lectura no es un "no" ni un "sí": es un "no se sabe".

    Operar aquí es el modo de fallo caro: los topes pueden estar sobre ellos y el
    sistema no lo comprobaría. 503 y no 403 porque el reintento tiene sentido.
    """
    market = MarketFalso(riesgo=RuntimeError("terminal cerrada"))
    svc = _servicio(market=market)
    cuerpo, status = _ejecuta(svc)
    assert status == 503
    assert cuerpo["status"] == "RISK_STATE_UNKNOWN"
    assert "terminal cerrada" in cuerpo["error"]
    assert "spec" not in market.llamadas


def test_un_estado_de_riesgo_con_error_dentro_tambien_bloquea():
    """El `error` puede venir DENTRO del dict en vez de ser una excepción.

    `daily_risk_state` lleva su error DENTRO del dict en vez de propagar la
    excepción, porque quien la llama la usa también para pintar un panel. Si el
    servicio solo mirara la excepción, este camino pasaría por delante de un estado
    que ya dice que no se pudo leer.
    """
    market = MarketFalso(riesgo={"blocked": False, "reasons": [],
                                 "error": "no se pudo leer el equity"})
    svc = _servicio(market=market)
    cuerpo, status = _ejecuta(svc)
    assert status == 503
    assert cuerpo["status"] == "RISK_STATE_UNKNOWN"
    assert "no se pudo leer el equity" in cuerpo["error"]


# ---------------------------------------------------------------------------
# 3. Noticias
# ---------------------------------------------------------------------------


def test_noticia_de_alto_impacto_bloquea():
    svc = _servicio(news=NewsFalso({"block": True, "reason": "NFP en 5 minutos",
                                     "fail_open": True}))
    cuerpo, status = _ejecuta(svc)
    assert status == 403
    assert cuerpo["status"] == "BLOCKED_BY_NEWS_GATE"
    assert "NFP" in cuerpo["error"]
    # Un `block=True` manda sobre el `fail_open`: el calendario que ha visto el
    # evento y además no está seguro de su veredicto NO deja pasar.
    assert "spec" not in svc._market.llamadas


def test_el_calendario_caido_falla_abierto_pero_lo_deja_escrito():
    """Un feed caído no puede dejar el sistema parado.

    Pero "se/ha podido comprobar" y "se ha operado sin comprobar" tienen que ser
    cosas distintas en la fila: la ausencia de veredicto es un dato, no un
    aprobado silencioso.
    """
    svc = _servicio(news=NewsFalso(RuntimeError("HTTP 503")))
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert cuerpo["news_gate"]["fail_open"] is True
    assert cuerpo["news_gate"]["aviso"]
    assert any("calendario" in w.lower() for w in cuerpo["warnings"])
    # Y la fila de auditoría lo dice con su propio veredicto, no con el del request.
    assert svc._store.filas[0]["verdict"] == VERDICT_IGNORED_NEWS


def test_sin_calendario_montado_no_avisa_nada():
    """Sin `news` no hay nada que comprobar, y eso no es un fallo abierto."""
    svc = _servicio(news=None)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert cuerpo["news_gate"] is None
    assert not any("calendario" in w.lower() for w in cuerpo.get("warnings", []))


def test_un_veredicto_negativo_pero_no_bloqueante_no_avisa():
    """`block=False` con un evento de riesgo bajo es un veredicto, no un aviso."""
    svc = _servicio(news=NewsFalso({"block": False, "fail_open": False,
                                     "detail": "solo riesgo medio"}))
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert cuerpo["news_gate"] is None


# ---------------------------------------------------------------------------
# 4. Spec y precio
# ---------------------------------------------------------------------------


def test_el_bróker_no_publica_el_simbolo_es_404():
    svc = _servicio(market=MarketFalso(spec=None))
    cuerpo, status = _ejecuta(svc)
    assert status == 404
    assert cuerpo["status"] == "SYMBOL_NOT_FOUND"


def test_spec_ilegible_es_503():
    svc = _servicio(market=MarketFalso(spec=RuntimeError("campo corrupto")))
    cuerpo, status = _ejecuta(svc)
    assert status == 503
    assert cuerpo["status"] == "SPEC_UNAVAILABLE"
    assert "campo corrupto" in cuerpo["error"]


def test_sin_cotizacion_no_se_manda_una_orden_a_mercado():
    """El peor modo de fallo posible: enviar a mercado sin saber a qué precio."""
    svc = _servicio(market=MarketFalso(precio=None))
    cuerpo, status = _ejecuta(svc)
    assert status == 503
    assert cuerpo["status"] == "PRICE_UNAVAILABLE"
    assert svc._store.filas == []  # ni rastro: no se intentó


def test_precio_no_positivo_tambien_es_precio_ausente():
    """Un 0 o un None disfrazado de precio es lo mismo que no tener precio."""
    svc = _servicio(market=MarketFalso(precio={"bid": 0.0, "ask": 0.0}))
    _, status = _ejecuta(svc)
    assert status == 503


def test_la_entrada_se_toma_del_lado_que_se_paga():
    """Una BUY entra al ASK; una SELL al BID.

    Entrar al bid en una compra es un precio que el bróker nunca ha cotizado, y la
    diferencia es exactamente el spread que el gate no vería.

    La SELL necesita SU invalidez, por encima de la entrada: `validate_entry`
    rechaza una SELL cuyo precio ya ha cruzado el nivel de invalidez, que es el
    otro lado del mismo razonamiento.
    """
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, action="BUY")
    assert status == 200, cuerpo
    assert cuerpo["entry"] == pytest.approx(1.10012)  # el ask
    assert puerto.envios[0]["sl"] == pytest.approx(1.10012 - 0.00100)

    puerto2 = PuertoFalso()
    svc2 = _servicio(puerto=puerto2)
    cuerpo2, status2 = _ejecuta(svc2, action="SELL", invalidate_level=1.10150)
    assert status2 == 200, cuerpo2
    assert cuerpo2["entry"] == pytest.approx(1.10000)  # el bid
    assert puerto2.envios[0]["sl"] == pytest.approx(1.10000 + 0.00100)


# ---------------------------------------------------------------------------
# 5. Distancias, lote y presupuesto
# ---------------------------------------------------------------------------


def test_sin_distancia_de_sl_no_se_inventa_el_stop():
    """Un stop inventado es una pérdida sin límite en el plan."""
    svc = _servicio(store=StoreFalso({"symbols_allow": ["EURUSD"], "min_rr": 2.0}))
    cuerpo, status = _ejecuta(svc, sl_distance=None)
    assert status == 400
    assert cuerpo["status"] == "NO_SL_DISTANCE"
    assert "sl_distance" in cuerpo["error"]
    assert svc._store.filas == []


def test_la_distancia_de_sl_se_toma_de_la_config_por_símbolo():
    store = StoreFalso({"symbols_allow": ["EURUSD"], "min_rr": 2.0,
                        "sl_distance_by_symbol": {"EURUSD": 0.00150}})
    puerto = PuertoFalso()
    svc = _servicio(store=store, puerto=puerto)
    cuerpo, status = _ejecuta(svc, sl_distance=None, tp_distance=0.00300)
    assert status == 200, cuerpo
    assert puerto.envios[0]["sl"] == pytest.approx(1.10012 - 0.00150)
    assert cuerpo["risk"]["sl_distance_source"] != "request"


def test_el_tp_por_defecto_es_el_r_multiple_configurado():
    puerto = PuertoFalso()
    svc = _servicio(store=StoreFalso(dict(CFG_MINIMA, tp_ratio_r=3.0)), puerto=puerto)
    cuerpo, status = _ejecuta(svc, tp_distance=None)
    assert status == 200, cuerpo
    # 1.0 de SL por 3.0 de TP.
    assert puerto.envios[0]["tp"] == pytest.approx(1.10012 + 0.00300)


def test_el_lote_se_dimensiona_por_riesgo_cuando_no_viene():
    """Sin volumen, la cuenta es la de la pantalla de lote: la MISMA.

    Con un SL de 0.00100 (10 pips, no 50) y tick value de 1.0 por tick y lote, un
    lote arriesga 100 ticks × 1.0 = 100. Al 0.5% de 10.000 son 50 de riesgo:
    50/100 = 0.50 lotes.
    """
    store = StoreFalso(dict(CFG_MINIMA, risk_pct=0.5))
    puerto = PuertoFalso()
    svc = _servicio(store=store, puerto=puerto)
    cuerpo, status = _ejecuta(svc, volume=None, score=90.0,
                              verdict="ALTA_PROBABILIDAD")
    assert status == 200, cuerpo
    assert cuerpo["risk"]["risk_pct"] == pytest.approx(0.5)
    assert puerto.envios[0]["volume"] == pytest.approx(0.50)
    assert any("Lote dimensionado por riesgo" in w for w in cuerpo["warnings"])
    # La nota tiene que cuadrar con lo que se calculó, no tener un hueco.
    nota = [w for w in cuerpo["warnings"] if "Lote dimensionado" in w][0]
    assert "50.00" in nota  # 0.5% de 10000


def test_un_verdict_que_no_es_alta_reduce_el_lote_a_la_mitad():
    """El riesgo del setup es parte de la decisión, no un adorno.

    Un verdict distinto de ALTA_PROBABILIDAD usa `reduced_risk_pct`, y si no está
    configurado, la mitad del nominal. Este test existe porque el valor por defecto
    de `_ejecuta` es un verdict que NO es alta: si la reducción se rompiera, los
    tests de lote seguirían verdes comparando contra el número que da el código.
    """
    puerto_nominal = PuertoFalso()
    svc_nominal = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)),
                            puerto=puerto_nominal)
    cuerpo_nominal, _ = _ejecuta(svc_nominal, volume=None,
                                 verdict="ALTA_PROBABILIDAD")

    puerto_reducido = PuertoFalso()
    svc_reducido = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)),
                             puerto=puerto_reducido)
    cuerpo, status = _ejecuta(svc_reducido, volume=None,
                              verdict="MEDIA_PROBABILIDAD")

    assert status == 200, cuerpo
    assert cuerpo_nominal["risk"]["risk_pct"] == pytest.approx(0.5)
    assert cuerpo["risk"]["risk_pct"] == pytest.approx(0.25)  # la mitad, sin configurar
    # Y el lote se reduce con el riesgo, no se queda igual por descuido.
    assert puerto_nominal.envios[0]["volume"] == pytest.approx(0.50)
    assert puerto_reducido.envios[0]["volume"] == pytest.approx(0.25)


def test_sin_balance_no_hay_dimensionado():
    svc = _servicio(market=MarketFalso(balance=None))
    cuerpo, status = _ejecuta(svc, volume=None)
    assert status == 400
    assert cuerpo["status"] == "LOT_UNAVAILABLE"
    assert "balance" in cuerpo["error"]


def test_el_riesgo_por_lote_se_pregunta_al_bróker_y_si_falla_se_declara():
    """`order_calc_profit` es la fuente; el tick value es la reserva.

    Y la fuente se DECLARA. La estimación y la medición del bróker producen las dos
    un número, así que deducir el origen de si el número existe etiquetaba de
    "broker" a una estimación hecha con el tick value: alguien leería una
    medición del bróker donde solo hubo aritmética.
    """
    puerto = PuertoFalso(riesgo_por_lote=RuntimeError("no disponible"))
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert cuerpo["risk"]["loss_per_lot_source"] == "estimacion"
    assert any("ESTIMADA" in w for w in cuerpo["warnings"])
    # 0.00100 de SL = 100 ticks de 0.00001, y 100 ticks x 1.0 por tick y LOTE = 100.
    # El `contract_size` (100.000) NO aparece: en MT5 el tick value ya lo incluye,
    # y multiplicarlo daba 10.000.000 por lote, que rechazaba todo por presupuesto.
    assert cuerpo["risk"]["loss_per_lot"] == pytest.approx(100.0)
    assert puerto.preguntas_riesgo, "se preguntó al bróker antes de estimar"


def test_el_tick_value_de_mt5_ya_incluye_el_tamano_del_contrato():
    """El invariante, dicho como test: riesgo por lote = ticks × tick_value.

    `SYMBOL_TRADE_TICK_VALUE` es el dinero que mueve la cuenta por un tick de UN
    lote. Si el cálculo de reserva multiplicara también por `contract_size`, en
    EURUSD el riesgo del lote saldría 100.000 veces alto y el gate de presupuesto
    rechazaría la operación entera con "riesgo excede el presupuesto" citando
    millones, que es un motivo que no lleva a ninguna parte: el número no sale de
    ningún sitio.

    Aquí el SL es 0.00100, o sea 100 ticks de 0.00001: 100 × 1.0 = 100 por lote.
    """
    puerto = PuertoFalso(riesgo_por_lote=None)  # el bróker no contesta
    svc = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)), puerto=puerto)
    cuerpo, status = _ejecuta(svc, volume=0.10)

    assert status == 200, cuerpo  # con el contrato multiplicado esto sería un 400
    assert cuerpo["risk"]["loss_per_lot"] == pytest.approx(100.0)
    # 100 por lote × 0.10 lotes = 10, contra un presupuesto de 0.5% de 10.000 = 50.
    assert cuerpo["risk"]["loss_per_lot"] * 0.10 == pytest.approx(10.0)


def test_una_perdida_que_supera_el_presupuesto_se_rechaza_aunque_el_bróker_la_diga():
    """El gate de riesgo manda sobre el bróker que dice que sí se puede enviar."""
    puerto = PuertoFalso(riesgo_por_lote=5000.0)  # 0.10 lotes = 500 > 50 de riesgo
    svc = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)), puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 400
    assert cuerpo["status"] == "GATE_REJECTED"
    assert any("excede el presupuesto" in r for r in cuerpo["reasons"])
    assert puerto.envios == []  # ni llegó a preguntarse


# ---------------------------------------------------------------------------
# 6. Los dos gates, y la razón de mirar los dos
# ---------------------------------------------------------------------------


def test_rr_insuficiente_no_sale():
    svc = _servicio(puerto=PuertoFalso())
    cuerpo, status = _ejecuta(svc, tp_distance=0.00100)  # R:R 1.0
    assert status == 400
    assert cuerpo["status"] == "GATE_REJECTED"
    assert any("R:R" in r for r in cuerpo["reasons"])
    assert svc._store.filas[0]["validated"] == 0


def test_estructura_rota_no_sale_aunque_el_rr_sea_perfecto():
    """Precio contra el nivel de invalidez: el setup ya no es ese."""
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, invalidate_level=1.10050)  # por encima del precio
    assert status == 400
    assert any("Estructura rota" in r for r in cuerpo["reasons"])
    assert puerto.envios == []


def test_deriva_de_entrada_no_sale():
    """La entrada se va 0.7 pips de más y el SL son 10: el 70% del stop.

    La deriva se mide contra el nivel PLANIFICADO, y para un BUY lo adverso es
    entrar por encima. Con la entrada real en el ask (1.10012), un planificado en
    1.09942 son 0.00070 de deriva: 70% del SL, sobre el 50% que admite la regla.
    """
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, planned_entry=1.09942)
    assert status == 400
    assert execution_quality.reason_code(cuerpo["reasons"][0]) == \
        execution_quality.REASON_DERIVA
    assert puerto.envios == []


def test_spread_que_se_come_el_stop_no_sale():
    """Spread de 50 pips contra un SL de 50: hay que recorrer 2x para breakeven."""
    puerto = PuertoFalso()
    svc = _servicio(market=MarketFalso(precio={"bid": 1.10000, "ask": 1.10050,
                                                "time": 1_700_000_000}),
                    puerto=puerto)
    cuerpo, status = _ejecuta(svc, planned_entry=1.10012)
    assert status == 400
    assert execution_quality.reason_code(cuerpo["reasons"][0]) == \
        execution_quality.REASON_SPREAD
    assert puerto.envios == []


def test_un_setup_rechazado_se_guarda_igual_para_poder_estudiarlo():
    """Un rechazo sin fila no se puede depurar: nadie sabe qué se rechazó ni por qué."""
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, tp_distance=0.00100)
    assert status == 400
    assert len(svc._store.filas) == 1
    fila = svc._store.filas[0]
    assert fila["validated"] == 0
    assert fila["reject_reasons"] == cuerpo["reasons"]
    assert fila["direction"] == "BUY"
    assert fila["entry"] == pytest.approx(1.10012)


def test_el_approved_de_un_gate_no_es_permiso_de_ejecutar():
    """Los dos tienen que decir que sí. Uno solo no basta.

    `execution_quality` aprueba una entrada sin deriva; `validate_entry` aprueba la
    estructura. Que los dos digan que sí la mitad es exactamente el fallo que produce
    órdenes vivas con una tesis que nadie ha mirado.
    """
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert cuerpo["execution_quality"]["approved"] is True
    assert cuerpo["risk_validation"]["approved"] is True


# ---------------------------------------------------------------------------
# 7. Auditoría antes del envío
# ---------------------------------------------------------------------------


def test_el_setup_se_escribe_ANTES_de_mandar_la_orden():
    """El orden de las dos llamadas es el que deja la historia creíble.

    Si el registro fuera después, un proceso que muere entre medias deja órdenes
    vivas sin fila, y una operación sin fila no sabe ni si ocurrió.
    """
    orden: List[str] = []

    class StoreOrdenado(StoreFalso):
        def log_setup(self, entry: Dict[str, Any]) -> int:
            orden.append("log_setup")
            return super().log_setup(entry)

    class PuertoOrdenado(PuertoFalso):
        def send_market_order(self, *a: Any, **kw: Any) -> Dict[str, Any]:
            orden.append("send_market_order")
            return super().send_market_order(*a, **kw)

    svc = _servicio(store=StoreOrdenado(), puerto=PuertoOrdenado())
    _ejecuta(svc)
    assert orden == ["log_setup", "send_market_order"]


def test_una_auditoria_que_falla_no_tumba_la_orden_pero_se_declara():
    """La auditoría no es requisito de ejecución, pero sí información obligatoria.

    Perder la fila permite operar; perder el AVISO de que se perdió la fila
    convierte "puedo auditar esto" en "creo que puedo auditar esto".
    """
    store = StoreFalso(log_setup_raises=True)
    puerto = PuertoFalso()
    svc = _servicio(store=store, puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert puerto.envios  # la orden SÍ salió
    assert cuerpo["setup_id"] is None
    assert cuerpo["audit_logged"] is False
    # Y sin fila no se intenta un update que no tiene a qué agarrarse.
    assert store.resultados == []


def test_los_intentos_de_llenado_se_graban_contra_la_misma_fila():
    """FOK falló y acabó en IOC: la fila tiene que contar los dos.

    Con un solo retcode final, el 10030 del FOK desaparecería y el rechazo
    parecería de un modo de llenado cualquiera.
    """
    store = StoreFalso()
    resultado = {"ok": True, "retcode": 10009, "status": "llenada", "fill_price": 1.10012,
                 "deal": 1, "order": 2, "filling": mt5_execution.ORDER_FILLING_IOC,
                 "volume": 0.10,
                 "attempts": [{"filling": 0, "retcode": 10030},
                              {"filling": 1, "retcode": 10009}]}
    puerto = PuertoFalso(resultado=resultado)
    svc = _servicio(store=store, puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert len(store.resultados) == 1
    guardado = store.resultados[0]
    assert guardado["setup_id"] == cuerpo["setup_id"]
    assert guardado["filling"] == mt5_execution.ORDER_FILLING_IOC
    assert [a["retcode"] for a in guardado["attempts"]] == [10030, 10009]
    assert guardado["retcode"] == 10009


# ---------------------------------------------------------------------------
# 8. Envío: lo que llega al puerto
# ---------------------------------------------------------------------------


def test_la_orden_llega_al_puerto_con_los_niveles_que_se_validaron():
    """El precio que sale de `order_send` es el que se ha medido y validado."""
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, magic=1234, comment="manual")
    assert status == 200, cuerpo
    envio = puerto.envios[0]
    assert envio["symbol"] == "EURUSD"
    assert envio["action"] == "BUY"
    assert envio["volume"] == pytest.approx(0.10)
    assert envio["sl"] == pytest.approx(SL_BUY)
    assert envio["tp"] == pytest.approx(TP_BUY)
    assert envio["magic"] == 1234
    assert envio["comment"] == "manual"
    # Y los mismos números en la respuesta que en el envío.
    assert cuerpo["entry"] == envio["sl"] + 0.001
    assert cuerpo["sl"] == envio["sl"]


def test_el_magic_y_el_comentido_salen_de_la_config_si_no_vienen():
    puerto = PuertoFalso()
    svc = _servicio(store=StoreFalso(), puerto=puerto)
    _ejecuta(svc, magic=None, comment=None)
    assert puerto.envios[0]["magic"] == 8882026
    assert puerto.envios[0]["comment"] == "ILOF Exec"


def test_sin_puerto_de_ejecucion_es_503_y_no_un_500_de_ultima_hora():
    """Se puede analizar sin poder operar, y eso tiene que decirse."""
    svc = _servicio(puerto=None)
    cuerpo, status = _ejecuta(svc)
    assert status == 503
    assert cuerpo["status"] == "NO_EXECUTION_PORT"
    # Aunque sin puerto, la fila queda: se intentó, y se sabe.
    assert len(svc._store.filas) == 1


def test_un_rechazo_del_bróker_no_es_un_200_con_ok_false():
    """El status HTTP distingue lo que el bróker rechazó de lo que falló el envío."""
    puerto = PuertoFalso(resultado={"ok": False, "status": "rejected", "retcode": 10019,
                                    "error": "invalid volume", "attempts": []})
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 400
    assert cuerpo["ok"] is False
    assert cuerpo["retcode"] == 10019
    assert cuerpo["error"] == "invalid volume"


def test_sin_terminal_es_503_para_que_el_cliente_reintente():
    """503 y no 400: un cliente que solo reintenta ante 5xx nunca reintentaría."""
    puerto = PuertoFalso(resultado={"ok": False, "status": "sin_terminal",
                                    "retcode": 10019, "error": "no hay terminal",
                                    "attempts": []})
    svc = _servicio(puerto=puerto)
    _, status = _ejecuta(svc)
    assert status == 503


def test_el_relleno_devuelto_es_el_del_bróker_no_el_pedido():
    """Si el bróker rellenó menos de lo pedido, se dice cuál de los dos."""
    puerto = PuertoFalso(resultado={"ok": True, "retcode": 10009, "status": "parcial",
                                    "fill_price": 1.10012, "volume": 0.04,
                                    "requested_volume": 0.10, "deal": 1, "order": 2,
                                    "filling": 0, "attempts": []})
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert cuerpo["volume"] == pytest.approx(0.04)
    assert cuerpo["requested_volume"] == pytest.approx(0.10)


def test_volumen_negativo_o_cero_se_recalcula_en_vez_de_mandarse():
    """Un 0 o un negativo no es un volumen: es la ausencia de volumen.

    Mandar un 0 al bróker es un rechazo con retcode propio; lo que se quiere es la
    cuenta de riesgo. El 0.25 sale de la mitad del nominal: `_ejecuta` usa un
    verdict que no es ALTA_PROBABILIDAD, y el riesgo reducido sin configurar es la
    mitad de 0.5%.
    """
    puerto = PuertoFalso()
    svc = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)), puerto=puerto)
    cuerpo, status = _ejecuta(svc, volume=0.0)
    assert status == 200, cuerpo
    assert puerto.envios[0]["volume"] == pytest.approx(0.25)

    puerto_neg = PuertoFalso()
    svc_neg = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)),
                        puerto=puerto_neg)
    _ejecuta(svc_neg, volume=-1.0)
    assert puerto_neg.envios[0]["volume"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 9. Cierre
# ---------------------------------------------------------------------------


def test_cierre_por_ticket_llega_al_puerto():
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = svc.close_position(ticket=987654)
    assert status == 200, cuerpo
    assert puerto.cierres[0]["ticket"] == 987654
    assert puerto.cierres[0]["magic"] == 8882026  # para no tocar otras operaciones


def test_cerrar_no_pasa_por_symbols_allow():
    """Una lista blanca de símbolos para ABRIR no puede impedir cerrar.

    Si lo hiciera, quitar el símbolo de la lista dejaría la posición viva sin
    ninguna manera de salir de ella, y el único remedio sería editar la config
    con la posición abierta.
    """
    store = StoreFalso({"symbols_allow": ["EURUSD"]})
    puerto = PuertoFalso()
    svc = _servicio(store=store, puerto=puerto)
    cuerpo, status = svc.close_position(ticket=1, symbol="DE40")
    assert status == 200, cuerpo
    assert puerto.cierres[0]["ticket"] == 1


def test_cerrar_no_necesita_ruta_abierta_porque_no_abre():
    """El cierre se salta el estado de riesgo del día a propósito.

    Los topes del día limitan abrir. Cerrar con el contador a tope es exactamente
    lo que hay que poder hacer: es la operación que reduce el riesgo.
    """
    market = MarketFalso(riesgo={"blocked": True, "reasons": ["tope diario"]})
    puerto = PuertoFalso()
    svc = _servicio(market=market, puerto=puerto)
    cuerpo, status = svc.close_position(ticket=1)
    assert status == 200, cuerpo
    assert "daily_risk_state" not in market.llamadas


def test_cerrar_una_posicion_que_no_existe_es_404():
    puerto = PuertoFalso(cierre={"ok": False, "status": "no_position",
                                 "error": "no hay posición 42"})
    svc = _servicio(puerto=puerto)
    cuerpo, status = svc.close_position(ticket=42)
    assert status == 404
    assert cuerpo["status"] == "no_position"


def test_cerrar_sin_ticket_ni_símbolo_no_toca_el_puerto():
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    _, status = svc.close_position()
    assert status == 400
    assert puerto.cierres == []


def test_cerrar_sin_puerto_es_503():
    svc = _servicio(puerto=None)
    cuerpo, status = svc.close_position(ticket=1)
    assert status == 503
    assert cuerpo["status"] == "NO_EXECUTION_PORT"


# ---------------------------------------------------------------------------
# 10. Utilidades del módulo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado,esperado", [
    ("sin_terminal", 503),
    ("symbol_not_found", 404),
    ("sin_precio", 503),
    ("volume_invalido", 400),
    ("rejected", 400),
    ("", 400),
    (None, 400),
])
def test_la_traduccion_de_estados_no_colapsa_todo_a_400(estado, esperado):
    assert _status_de_ejecucion(estado) == esperado


def test_execution_from_toma_las_piezas_del_runtime():
    """El servicio se monta desde el runtime, no desde el handler.

    Que sea una función y no un `if hasattr` en cada ruta es lo que garantiza que
    el watcher y la API tengan las MISMAS puertas.
    """

    class RuntimeFalso:
        market = "market"
        store = "store"
        news = "news"
        execution = "execution"
        reloj = "reloj"

    svc = execution_from(RuntimeFalso())
    assert isinstance(svc, ExecutionService)
    assert svc._market == "market"
    assert svc._store == "store"
    assert svc._news == "news"
    assert svc._execution == "execution"


def test_execution_from_tolera_un_runtime_sin_puerto_de_ejecucion():
    """Un runtime de solo lectura da un servicio que NO opera, no uno que revienta."""

    class RuntimeFalso:
        market = "market"
        store = "store"
        news = "news"

    svc = execution_from(RuntimeFalso())
    assert svc._execution is None
    assert svc._reloj is None
