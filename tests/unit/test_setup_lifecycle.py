"""La máquina de estados del setup, sin base de datos y sin reloj.

Aquí se prueba lo que decide si el watcher **vuelve a hablar**: cuándo un setup ya
detectado se reavisa, cuándo se cierra y cuándo se considera el mismo de antes.

Por qué merece un fichero propio de tests
-----------------------------------------
El fallo caro de esta lógica no es que avise de más o de menos: es que un `None` mal
interpretado hace que el setup **nunca caduque**. Si cada escaneo reescribe la fila de
estado, el `created_at` se renueva en cada ciclo, el TTL se mide desde hace un segundo y
el setup sigue "activo" mientras el mercado se ha ido tres horas. Eso no salta en ningún
test de la ruta: la ruta devuelve `200` y los eventos son los correctos. Solo se ve en la
tabla de estados, y por eso la tabla tiene sus propios tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from core import setup_lifecycle

AHORA = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _fila(status: str = "", creado: Optional[datetime] = None,
          **extra: Any) -> Dict[str, Any]:
    momento = AHORA if creado is None else creado
    fila: Dict[str, Any] = {
        "symbol": "EURUSD",
        "timeframe": "M15",
        "status": status,
        "created_at": momento.isoformat(),
    }
    fila.update(extra)
    return fila


class TestDecideTransition:
    def test_sin_ciclo_previo_y_gate_ok_abre_ciclo(self):
        assert setup_lifecycle.decide_transition(None, True, ahora=AHORA) == "new"

    def test_sin_ciclo_previo_y_gate_rechazado_no_hace_nada(self):
        """`None` no es "setup malo": es "no hay nada que hacer".

        Si el escaneo tratara este `None` como un rechazo escribiría una fila de descarte
        para un símbolo que ni siquiera tiene ciclo abierto, y el histórico de rechazos
        contaría como ruido algo que fue una ausencia de decisión.
        """
        assert setup_lifecycle.decide_transition(None, False, ahora=AHORA) is None

    @pytest.mark.parametrize("status", list(setup_lifecycle.ESTADOS_TERMINADOS))
    def test_un_ciclo_terminado_permite_abrir_otro(self, status):
        """Tras cerrarse, el siguiente setup es un setup NUEVO, no un recargo.

        Sin esto, un símbolo que alterna setups válidos e inválidos se queda atascado en
        el segundo forever: la fila vieja dice "active" y el gate nuevo no puede
        reavisar porque ya hay un ciclo abierto.
        """
        fila = _fila(status=status)
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA) == "new"

    def test_un_setup_activo_y_valido_no_reavisa(self):
        """El caso normal: el mismo setup de hace treinta segundos, ya avisado.

        Es el `None` que más caro sale si se implementa mal. Escribir la fila aquí
        renueva `created_at`, el TTL se mide desde ahora y el setup no caduca nunca.
        """
        fila = _fila(status="active")
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA) is None

    def test_un_setup_activo_que_muere_se_cierra(self):
        assert setup_lifecycle.decide_transition(_fila("active"), False, ahora=AHORA) == "closed"

    def test_un_setup_activo_caduca_por_ttl(self):
        """El TTL se mide desde `created_at`, no desde el último escaneo."""
        viejo = _fila("active", creado=AHORA - timedelta(seconds=3000))
        assert setup_lifecycle.decide_transition(viejo, True, ahora=AHORA,
                                                 ttl_sec=2700.0) == "closed"

    def test_un_setup_activo_dentro_del_ttl_no_cierra(self):
        """El borde del TTL no es el TTL.

        Con `created_at` justo a 2699 s y `ttl_sec=2700` el setup sigue vigente. El
        instante exacto (2700.0) tampoco cierra, porque `>` es estricto: el umbral es
        "más de 45 minutos", no "45 minutos y un microsegundo".
        """
        justo = _fila("active", creado=AHORA - timedelta(seconds=2699))
        assert setup_lifecycle.decide_transition(justo, True, ahora=AHORA,
                                                 ttl_sec=2700.0) is None
        exacto = _fila("active", creado=AHORA - timedelta(seconds=2700))
        assert setup_lifecycle.decide_transition(exacto, True, ahora=AHORA,
                                                 ttl_sec=2700.0) is None

    def test_sin_created_at_no_caduca_nunca(self):
        """Una fila sin fecha no puede estar expirada.

        Declararla caducada cerraría un setup vivo por un campo de auditoría vacío. En
        el store real `created_at` siempre viene, pero `update_setup_state` crea filas
        mínimas sin dirección ni fecha cuando solo registra una transición, y esa fila
        existe.
        """
        fila = _fila(status="active")
        fila["created_at"] = None
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA) is None

    def test_un_created_at_ilegible_no_caduca(self):
        fila = _fila(status="active")
        fila["created_at"] = "no-es-una-fecha"
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA) is None

    def test_created_at_sin_zona_se_lee_como_utc(self):
        """Una fecha naive son las 12:00 UTC por convenio del store.

        Si se comparara naive contra aware saltaría `TypeError` en el escaneo entero: el
        fallo no está en el dato raro, está en que `created_at` se guardó sin `Z`.
        """
        fila = _fila(status="active", creado=datetime(2026, 1, 1, 12, 0))
        viejo = _fila(status="active", creado=datetime(2026, 1, 1, 11, 0))
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA,
                                                 ttl_sec=2700.0) is None
        assert setup_lifecycle.decide_transition(viejo, True, ahora=AHORA,
                                                 ttl_sec=2700.0) == "closed"

    def test_created_at_con_z_explicita(self):
        fila = _fila(status="active")
        fila["created_at"] = "2026-01-01T11:00:00Z"
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA,
                                                 ttl_sec=2700.0) == "closed"

    def test_ttl_cero_no_caduca(self):
        """`dedup_ttl_sec: 0` significa "sin caducidad", no "caduca ya".

        Es un valor que se puede escribir en `strategy.yaml` sin ninguna validación que
        lo impida, y leerlo como caducidad inmediata apagaría los set ups de un operador
        que solo quería silenciar el TTL.
        """
        viejo = _fila("active", creado=AHORA - timedelta(days=30))
        assert setup_lifecycle.decide_transition(viejo, True, ahora=AHORA, ttl_sec=0.0) is None

    def test_news_ignored_no_reavisa_mientras_la_señal_siga_viva(self):
        """El blackout ya se registró: reavisar sería contar el mismo evento cada ciclo."""
        fila = _fila(status="news_ignored")
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA) is None

    def test_news_ignored_cierra_cuando_la_señal_muere(self):
        assert setup_lifecycle.decide_transition(_fila("news_ignored"), False,
                                                 ahora=AHORA) == "closed"

    def test_news_ignored_no_revive_por_caducidad_del_ttl_sin_señal(self):
        viejo = _fila("news_ignored", creado=AHORA - timedelta(seconds=4000))
        assert setup_lifecycle.decide_transition(viejo, True, ahora=AHORA,
                                                 ttl_sec=2700.0) == "closed"

    def test_ejecutado_no_se_vuelve_a_ejecutar(self):
        """Ya hay una orden abierta: mientras el setup siga vivo, no se manda otra."""
        fila = _fila(status="executed")
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA) is None

    def test_ejecutado_cierra_cuando_la_señal_muere(self):
        """El cierre es lo que permite que el siguiente sea un ciclo nuevo."""
        assert setup_lifecycle.decide_transition(_fila("executed"), False,
                                                 ahora=AHORA) == "closed"

    def test_ejecutado_no_expira_por_ttl(self):
        """El TTL es del ciclo de ALERTA. Una posición abierta no caduca por un reloj.

        Si el TTL cerrara un setup ejecutado, el siguiente setup válido del símbolo se
        avisaría como nuevo con la posición anterior aún viva, y el operador vería dos
        señales del mismo mercado con una ya abierta.
        """
        viejo = _fila("executed", creado=AHORA - timedelta(days=7))
        assert setup_lifecycle.decide_transition(viejo, True, ahora=AHORA,
                                                 ttl_sec=2700.0) is None

    def test_un_estado_desconocido_no_hace_nada(self):
        """Un estado que este código no conoce no se reescribe.

        Predecir una transición desde un estado que no se reconoce es escribir sobre un
        dato que otro proceso escribió. Es peor no hacer nada que sobrescribirlo.
        """
        assert setup_lifecycle.decide_transition(_fila("medio_ejecutado"), True,
                                                 ahora=AHORA) is None

    def test_el_estado_se_compara_en_minusculas_y_sin_espacios(self):
        """`store` lo escribe en minúsculas, pero el estado viene de una columna.

        `"Active"` y `" active "` no son estados raros: son el mismo estado escrito por
        alguien que no miró la convención, y tratarlos como desconocidos congelaría el
        ciclo para siempre sin avisar.
        """
        for status in ("Active", " ACTIVE ", "news_Ignored"):
            fila = _fila(status=status)
            resultado = setup_lifecycle.decide_transition(fila, False, ahora=AHORA)
            assert resultado == "closed", status

    def test_acepta_fila_por_atributos(self):
        """Una fila que no sea dict (una tupla de la consulta) no rompe el escaneo.

        `store.get_setup_state` devuelve dict, pero el contrato de esta función es "una
        fila", y un `AttributeError` al leer el estado de un símbolo solo se vería en
        producción con la base de datos ya poblada por la versión anterior.
        """

        class Fila:
            status = "active"
            created_at = (AHORA - timedelta(seconds=100)).isoformat()

        assert setup_lifecycle.decide_transition(Fila(), True, ahora=AHORA) is None


class TestFirmaDeRechazo:
    def test_el_numero_del_score_no_distingue_dos_rechazos_iguales(self):
        """58.3 y 55.1 son el mismo motivo con dos decimales distintos.

        Si la firma los distinguiera, un setup que lleva media hora por debajo del
        umbral escribiría una fila cada `scan_interval_sec`.
        """
        a = {"direction": "BUY", "score": 58.3, "reasons": ["Score 58.3 < umbral 70.0."]}
        b = {"direction": "BUY", "score": 55.1, "reasons": ["Score 55.1 < umbral 70.0."]}
        assert setup_lifecycle.firma_de_rechazo(a) == setup_lifecycle.firma_de_rechazo(b)

    def test_la_banda_de_5_puntos_separa_un_cambio_de_veredicto(self):
        """58 (lejos) y 62 (cerca) son dos hechos distintos aunque ambos rechacen.

        Un rechazo que sube de banda se registra otra vez: el operador tiene que poder
        ver que el setup se está acercando al umbral, y no solo que "sigue sin entrar".
        """
        a = {"direction": "BUY", "score": 58.0, "reasons": ["Score 58.0 < umbral 70.0."]}
        b = {"direction": "BUY", "score": 62.0, "reasons": ["Score 62.0 < umbral 70.0."]}
        assert setup_lifecycle.firma_de_rechazo(a) != setup_lifecycle.firma_de_rechazo(b)

    def test_la_direccion_distingue_los_lados(self):
        a = {"direction": "BUY", "score": 55.0, "reasons": []}
        b = {"direction": "SELL", "score": 55.0, "reasons": []}
        assert setup_lifecycle.firma_de_rechazo(a) != setup_lifecycle.firma_de_rechazo(b)

    def test_un_motivo_nuevo_es_otra_firma(self):
        """Añadir un motivo (p. ej. la zona quedó lejos) vuelve a registrar."""
        antes = {"direction": "BUY", "score": 55.0, "reasons": ["Fuera de killzone."]}
        despues = {"direction": "BUY", "score": 55.0,
                   "reasons": ["Fuera de killzone.", "Zona de entrada inalcanzable."]}
        assert setup_lifecycle.firma_de_rechazo(antes) != setup_lifecycle.firma_de_rechazo(despues)

    def test_un_gate_vacio_no_revienta(self):
        """`evaluate_gate` devuelve `None` sin scores y quien llama filtra antes.

        Aun así, la firma de un `{}` tiene que ser una tupla comparable: si lanzara
        `AttributeError` al desduplicar, el rechazo —que es lo que se estaba auditing—
        se pierde justo en el camino que lo registra.
        """
        firma = setup_lifecycle.firma_de_rechazo(None)
        assert firma == setup_lifecycle.firma_de_rechazo({})

    def test_un_score_ilegible_no_revienta(self):
        firma = setup_lifecycle.firma_de_rechazo({"direction": "BUY", "score": "mucho"})
        assert firma[0] == "BUY"


class TestClaveDeMotivo:
    def test_saca_los_numeros(self):
        assert setup_lifecycle.clave_de_motivo("Score 58.3 < umbral 70.0.") == "Score  < umbral "

    def test_un_motivo_vacio_no_revienta(self):
        assert setup_lifecycle.clave_de_motivo(None) == ""


class TestComponentesDelScore:
    def test_trae_los_componentes_del_lado_ganador(self):
        """Del lado que ganó, no de los dos: el breakdown es la evidencia del número
        que motivó la decisión."""
        snap = {
            "risk_engine": {
                "bull": {"score": 70.0, "components": {"killzone": {"in_killzone": True}}},
                "bear": {"score": 30.0, "components": {"killzone": {"in_killzone": False}}},
            }
        }
        comps = setup_lifecycle.componentes_del_score(snap, "BUY")
        assert comps["killzone"]["in_killzone"] is True

    def test_sin_ese_lado_devuelve_vacio_y_no_inventa(self):
        snap = {"risk_engine": {"bull": {"score": 70.0, "components": {"killzone": {}}}}}
        assert setup_lifecycle.componentes_del_score(snap, "SELL") == {}

    def test_una_direccion_desconocida_no_adivina_el_lado(self):
        """"HOLD" no es un lado del snapshot, y no se traduce a ninguno.

        La comparación no distingue mayúsculas (la dirección llega de etiquetas de varias
        fuentes), pero una dirección que no es BUY ni SELL no se adivina.
        """
        snap = {"risk_engine": {"bull": {"score": 70.0, "components": {"killzone": {}}}}}
        for direccion in ("", None, "HOLD", "long"):
            assert setup_lifecycle.componentes_del_score(snap, direccion) == {}

    def test_la_direccion_no_distingue_mayusculas(self):
        snap = {"risk_engine": {"bear": {"score": 70.0, "components": {"killzone": {"name": "NY"}}}}}
        assert setup_lifecycle.componentes_del_score(snap, "sell") == {"killzone": {"name": "NY"}}

    def test_no_altera_el_snapshot_aunque_se_anada(self):
        """El anidamiento se copia entero, no solo el nivel superior.

        Una copia superficial bastaría mientras nadie mutara los dicts anidados, y eso es
        justo lo que no se puede comprobar leyendo el resto del fichero.
        """
        comps_originales = {"killzone": {"in_killzone": True}}
        snap = {"risk_engine": {"bull": {"score": 70.0, "components": comps_originales}}}
        salida = setup_lifecycle.componentes_del_score(snap, "BUY")
        salida["killzone"]["in_killzone"] = False
        salida["smc"] = {}
        assert comps_originales["killzone"]["in_killzone"] is True
        assert "smc" not in comps_originales

    def test_snapshot_sin_risk_engine(self):
        assert setup_lifecycle.componentes_del_score({}, "BUY") == {}
