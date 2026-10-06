"""`strategy.yaml` materializado: la parte pura y la parte que toca el fichero.

Por qué el archivo se parte en dos (y por qué hay dos clases de test):

- `core/strategy.py` es lógica pura y su test no puede abrir ficheros (el guard de
  `test_core_purity.py` lo prohíbe). Aquí se le pasa el TEXTO.
- `settings/strategy_source.py` es la mitad que lee y escribe, y su test sí toca
  disco, pero **siempre un temporal**: el `strategy.yaml` de producción decide si el
  sistema opera, y un test que lo pisa no falla, se_limit de tres formas distintas.

Lo que se cubre, en orden de importancia:

  1. Que la config plana sale del YAML REAL que se reparte, y no de un invento. Un
     test con un YAML de laboratorio pasa igual con un aplanado que se comiese la
     sección `execution` entera; contra el YAML de verdad, si el shape se rompe, el
     `min_score` o la distancia de SL dejan de aparecer.
  2. Que una regla rota LANZA en vez de degradarse. Un `risk_pct: "mucho"` que se
     convierte a 0.5 es un sistema que opera al 0,1% de riesgo y no lo dice.
  3. Que escribir una clave no destruye el resto del archivo: los comentarios del
     YAML son la documentación de las reglas y se necesiten para entender por qué
     un número es el que es.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from core import paths as core_paths
from core import strategy as st
from settings import strategy_source

YAML_REAL = Path(core_paths.STRATEGY_PATH)


def _doc_real() -> dict:
    with open(YAML_REAL, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@pytest.fixture()
def yaml_tmp(tmp_path, monkeypatch):
    """Un `strategy.yaml` temporal, con la caché del source vaciada.

    Es el patrón de `store.DB_PATH`: se redirige el atributo del módulo, no
    `core.paths`, para que nada pueda escribir el archivo de producción por
    accidente.
    """
    target = tmp_path / "strategy.yaml"
    target.write_text(
        "risk:\n"
        "  risk_pct: 0.5  # por operación\n"
        "execution:\n"
        "  magic: 8882026\n"
        "  symbols_allow: [\"EURUSD\"]\n"
        "score:\n"
        "  min_score: 59.5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(strategy_source, "STRATEGY_PATH", str(target))
    strategy_source.invalidate()
    yield target
    strategy_source.invalidate()


# ---------------------------------------------------------------------------
# core/strategy.py: la parte pura
# ---------------------------------------------------------------------------


class TestBuildFlatContraElYamlReal:
    """El shape de la config, verificado contra el archivo que se reparte."""

    @pytest.fixture(autouse=True)
    def _flat(self):
        self.doc = _doc_real()
        self.flat = st.build_flat(self.doc)

    def test_las_claves_de_ejecucion_llegan_en_minusculas(self):
        e = self.doc["execution"]
        assert self.flat["magic"] == e["magic"]
        assert self.flat["comment"] == e["comment"]
        assert self.flat["sl_distance"] == e["sl_distance"]
        assert self.flat["tp_ratio_r"] == e["tp_ratio_r"]
        assert self.flat["symbols_allow"] == e["symbols_allow"]

    def test_el_mapa_de_sl_por_simbolo_llega_completo(self):
        """El mapa por símbolo es el que evita aplicar 0.0012 al oro.

        Si el aplanado lo perdiera, el SL se resolvería por el global y el sistema
        operaría con un stop 2500 veces más corto en XAUUSD sin decir nada.
        """
        por_simbolo = self.flat["sl_distance_by_symbol"]
        assert por_simbolo == self.doc["execution"]["sl_distance_by_symbol"]
        assert por_simbolo["XAUUSD"] == 3.0

    def test_score_y_prop_aplanan_a_sus_claves_planas(self):
        assert self.flat["min_score"] == self.doc["score"]["min_score"]
        assert self.flat["min_rr"] == self.doc["score"]["min_rr"]
        assert self.flat["setup_ttl_minutes"] == self.doc["score"]["setup_ttl_minutes"]
        assert self.flat["prop_enabled"] is self.doc["prop"]["enabled"]
        assert self.flat["prop_max_dd_daily_pct"] == self.doc["prop"]["max_dd_daily_pct"]

    def test_los_pesos_y_las_killzones_salen_como_json_string(self):
        """Es el shape histórico: los consumidores hacen `json.loads(...)`.

        Publicarlos como dict rompería `risk_engine` y la UI a la vez, y pasaría
        inadvertido en cualquier test que solo mirara `cfg["killzones"]`.
        """
        assert json.loads(self.flat["risk_weights"]) == self.doc["score"]["weights"]
        assert json.loads(self.flat["killzones"]) == self.doc["killzones"]

    def test_las_fuentes_de_datos_y_el_watcher_salen_como_datos(self):
        assert self.flat["data_sources"] == self.doc["data_sources"]
        assert self.flat["watcher_config"]["scan_interval_sec"] == (
            self.doc["watcher"]["scan_interval_sec"]
        )
        assert {s["symbol"] for s in self.flat["watcher_config"]["symbols"]} == {
            s["symbol"] for s in self.doc["watcher"]["symbols"]
        }

    def test_lo_que_el_yaml_no_dice_cae_en_los_defaults(self):
        """Sin sección `agent`, los textos del agente son cadenas vacías.

        Si aqui se devolviera `None`, el prompt del rol se armaría con un "None"
        pegado al system prompt en vez de con la política vacía.
        """
        assert self.flat["agent_system_prompt"] == ""
        assert self.flat["agent_risk_policy"] == ""
        assert self.flat["agent_topics"] == st.DEFAULTS["agent_topics"]


class TestResolversDePesosYKillzones:
    """Las dos formas de la misma configuración, y por qué hay que leer las dos.

    `build_flat` aplana el YAML y serializa a JSON string los mapas y las listas que un
    escalar en línea no puede expresar (`risk_weights`, `killzones`). Quien los lee sin
    deserializar reventaba dentro de `killzone_score` con
    `AttributeError: 'str' object has no attribute 'get'`.

    Pero el `strategy.yaml` sin aplanar también circula por el repositorio (`BrokerClock`,
    `StrategySource`), así que un resolver que solo acepta la forma plana deja ese
    camino con los defaults en silencio. Y un default silencioso es indistinguible de
    una decisión del usuario.
    """

    ANIDADO = {
        "score": {"weights": {"cot": 0.0, "cvd_of": 20.0, "smc": 50.0,
                              "killzone": 0.0, "smr_dxy": 15.0}},
        "killzones": [{"name": "Londres", "start": "07:00", "end": "10:00"}],
    }

    def test_el_plano_y_el_anidado_dan_lo_mismo(self):
        plana = st.build_flat(self.ANIDADO)

        assert st.risk_weights_for(plana) == st.risk_weights_for(self.ANIDADO)
        assert st.killzones_for(plana) == st.killzones_for(self.ANIDADO)

    def test_los_pesos_vienen_como_json_string_del_plano(self):
        """El reproductor del bug, encerrado: si esto deja de ser un string, el test
        de la otra forma está probando otra cosa y no la que falla en producción."""
        plana = st.build_flat(self.ANIDADO)

        assert isinstance(plana["risk_weights"], str)
        assert isinstance(plana["killzones"], str)
        assert st.risk_weights_for(plana)["smc"] == 50.0

    def test_una_config_ya_parseada_tambien_valida(self):
        """Un test, un doble o un consumidor futuro puede no serializar: se acepta."""
        cfg = {"risk_weights": {"smc": 50.0}, "killzones": self.ANIDADO["killzones"]}

        assert st.risk_weights_for(cfg)["smc"] == 50.0
        assert st.killzones_for(cfg)[0]["start"] == "07:00"

    def test_sin_config_devuelve_none_y_no_un_default_fingido(self):
        """`None` deja que el motor aplique sus defaults y PERMITE avisar. `{}` haría
        lo mismo diciendo que la configuración dijo algo."""
        assert st.risk_weights_for({}) is None
        assert st.risk_weights_for(None) is None
        assert st.killzones_for({}) is None
        assert st.killzones_for(None) is None

    def test_basura_devuelve_none_en_vez_de_reventar(self):
        """Un JSON roto en la config no puede tumbar el snapshot entero."""
        for basura in ("no es json", "[1, 2]", 42, {"smc": "mucho"}, {"smc": True}):
            assert st.risk_weights_for({"risk_weights": basura}) is None
        for basura in ("no es json", "{}", 42, {"start": "07:00"}, [{"start": 7, "end": "10:00"}]):
            assert st.killzones_for({"killzones": basura}) is None

    def test_acepta_la_forma_agrupada_por_clave(self):
        """`{"windows": [...]}` es como se guarda el bloque cuando viene agrupado."""
        cfg = {"killzones": {"windows": [{"name": "NY", "start": "12:30", "end": "15:30"}]}}

        assert st.killzones_for(cfg) == [{"name": "NY", "start": "12:30", "end": "15:30"}]

    def test_una_ventana_sin_horas_no_entra(self):
        """Media ventana no es una ventana: aceptarla daría `in_killzone` sin poder
        comparar contra una hora."""
        cfg = {"killzones": [{"name": "rota"}, {"name": "NY", "start": "12:30", "end": "15:30"}]}

        assert st.killzones_for(cfg) == [{"name": "NY", "start": "12:30", "end": "15:30"}]

    def test_el_yaml_de_produccion_se_resuelve_completo(self):
        """Contra el archivo real, no contra un invento: si `build_flat` dejara de
        serializar algo, el resolver devolvería `None` y el score mediría con los
        defaults sin que nadie lo notara."""
        doc = _doc_real()

        assert st.risk_weights_for(st.build_flat(doc)) == st.risk_weights_for(doc)
        assert st.killzones_for(st.build_flat(doc)) == st.killzones_for(doc)
        assert st.risk_weights_for(doc)
        assert st.killzones_for(doc)


class TestReglasInvalidasLanzan:

    """Un valor roto para el motor: mejor no arrancar que operar con la regla rota."""

    @pytest.mark.parametrize(
        "mutar, motivo",
        [
            (lambda d: d["risk"].__setitem__("risk_pct", "mucho"), "debe ser un número"),
            (lambda d: d["risk"].__setitem__("risk_pct", 500), "no puede ser mayor que 100"),
            (lambda d: d["score"].__setitem__("min_score", -1), "no puede ser menor que 0"),
            (lambda d: d["execution"].__setitem__("comment", 42), "debe ser un texto"),
            (lambda d: d["execution"].__setitem__("symbols_allow", "EURUSD"), "lista de símbolos"),
            (lambda d: d["execution"].__setitem__("sl_distance", True), "debe ser un número"),
            (lambda d: d["execution"]["sl_distance_by_symbol"].__setitem__("XAUUSD", 0), "distancia > 0"),
            (lambda d: d["prop"].__setitem__("enabled", "sí"), "true/false"),
            (lambda d: d["data_sources"].__setitem__("cot", "on"), "debe ser true/false"),
            (lambda d: d["score"]["weights"].__setitem__("smc", -5), "número >= 0"),
            (lambda d: d["watcher"].__setitem__("auto_execute", 1), "true/false"),
            (lambda d: d["watcher"].__setitem__("scan_interval_sec", -30), "número >= 0"),
            (lambda d: d.__setitem__("killzones", [{"name": "LON"}]), "HH:MM"),
        ],
    )
    def test_build_flat_explota_con_el_motivo_en_el_mensaje(self, mutar, motivo):
        doc = _doc_real()
        mutar(doc)
        with pytest.raises(st.StrategyConfigError) as exc:
            st.build_flat(doc)
        assert motivo in str(exc.value)

    def test_yaml_no_parseable_tambien_es_error_de_config(self):
        """Sintaxis rota -> `StrategyConfigError`, no el error crudo de PyYAML.

        Quien carga la config necesita una sola excepción que capturar; si sale el
        `YAMLError` de PyYAML, cada llamante tiene que conocer la librería que lee el
        fichero.
        """
        with pytest.raises(st.StrategyConfigError):
            st.load_doc("risk:\n  risk_pct: [0.5\n")

    def test_una_raiz_que_no_es_mapa_no_es_config(self):
        with pytest.raises(st.StrategyConfigError, match="raíz"):
            st.load_doc("- risk_pct: 0.5\n")

    def test_yaml_vacio_es_un_documento_vacio(self):
        assert st.load_doc("") == {}


class TestMinScoreYSlDistance:
    def test_un_min_score_explicito_en_cero_no_cae_al_suelo(self):
        """`cfg.get("min_score") or 80.0` convertía un 0 pedido en 80.

        0.0 es legal (rango 0..100) y significa "no exigir score", que es una
        decisión de un humano. Con el `or`, esa decisión se sustituía en silencio.
        """
        assert st.min_score_for({}) == st.DEFAULT_MIN_SCORE
        assert st.min_score_for({"min_score": None}) == st.DEFAULT_MIN_SCORE
        assert st.min_score_for({"min_score": 0.0}) == 0.0
        assert st.min_score_for({"min_score": "61.5"}) == 61.5
        assert st.min_score_for({"min_score": True}) == st.DEFAULT_MIN_SCORE

    def test_la_distancia_gana_por_simbolo_sobre_el_global(self):
        cfg = {"sl_distance": 0.0012, "sl_distance_by_symbol": {"XAUUSD": 3.0}}
        assert st.sl_distance_for(cfg, "XAUUSD") == (3.0, "symbol")
        assert st.sl_distance_for(cfg, "xauusd") == (3.0, "symbol")
        assert st.sl_distance_for(cfg, "EURUSD") == (0.0012, "global")

    def test_el_origen_global_es_visible_para_la_ui(self):
        """Un símbolo que no pidió entrada hereda el global, y la UI tiene que poder
        avisar: 0.0012 son 12 pips en EURUSD y un stop de risa en oro."""
        cfg = {"sl_distance": 0.0012, "sl_distance_by_symbol": {}}
        assert st.sl_distance_for(cfg, "XAUUSD") == (0.0012, "global")

    def test_sin_distancia_utilizable_devuelve_none_y_no_inventa(self):
        """Sin distancia no hay un número que devolver: simular 0.0012 en oro es peor
        que no devolver nada."""
        assert st.sl_distance_for({"sl_distance": 0}, "EURUSD") == (None, "none")
        assert st.sl_distance_for({}, "EURUSD") == (None, "none")

    def test_la_clave_legacy_en_pips_solo_con_spec(self):
        class Spec:
            pip = 0.0001

        cfg = {"sl_default_pips": 30}
        assert st.sl_distance_for(cfg, "EURUSD", Spec()) == (0.003, "legacy_pips")
        assert st.sl_distance_for(cfg, "EURUSD", None) == (None, "none")


class TestPatchText:
    """El parche de una línea: el YAML se sigue leyendo después de guardarlo."""

    def test_cambia_el_valor_y_conserva_el_comentario_inline(self):
        texto = "risk:\n  risk_pct: 0.5  # por operación\n"
        out = st.patch_text(texto, "risk", "risk_pct", 0.75)
        assert "risk_pct: 0.75" in out
        assert "# por operación" in out
        assert st.load_doc(out)["risk"]["risk_pct"] == 0.75

    def test_no_toca_una_clave_igual_de_otra_seccion(self):
        """`magic` solo existe en `execution`; si la búsqueda no paro en la sección,
        cambiaría la primera coincidencia del archivo."""
        texto = "risk:\n  comment: A\nexecution:\n  comment: B\n"
        out = st.patch_text(texto, "execution", "comment", "C")
        assert out.splitlines()[1] == "  comment: A"
        assert out.splitlines()[3] == "  comment: C"

    def test_no_conda_comentarios_que_parecen_secciones(self):
        texto = "# execution:\nrisk:\n  risk_pct: 0.5\n"
        out = st.patch_text(texto, "execution", "magic", 7)
        assert "# execution:" in out
        assert "  magic: 7" in out

    def test_inserta_el_subkey_que_falta_bajo_la_cabecera(self):
        texto = "score:\n  min_score: 59.5\n"
        out = st.patch_text(texto, "score", "min_rr", 2.5)
        assert st.load_doc(out)["score"] == {"min_score": 59.5, "min_rr": 2.5}

    def test_crea_la_seccion_si_no_existe(self):
        out = st.patch_text("risk:\n  risk_pct: 0.5\n", "prop", "max_dd_daily_pct", 3.0)
        assert st.load_doc(out)["prop"] == {"max_dd_daily_pct": 3.0}

    def test_los_escapes_no_abren_una_clave_nueva(self):
        """Un valor con salto de línea en YAML plano se traga la clave siguiente.

        Es un texto que llega de la UI (el comentario del broker), así que el caso
        no es inventado: sin escape, guardar `Acme\nmin_score: 1` deja el YAML con
        un `min_score` que nadie escribió.
        """
        out = st.patch_text("execution:\n  comment: X\n", "execution", "comment", "A\nB: 1")
        doc = st.load_doc(out)
        assert doc["execution"]["comment"] == "A\nB: 1"
        assert "min_score" not in doc.get("score", {})

    @pytest.mark.parametrize(
        "valor, esperado",
        [
            (True, "true"),
            (False, "false"),
            (None, "null"),
            (2.5, "2.5"),
            (7, "7"),
            ("EURUSD", "EURUSD"),
            ("Web Exec", '"Web Exec"'),
            (["EURUSD"], "[EURUSD]"),
        ],
    )
    def test_yaml_scalar(self, valor, esperado):
        assert st.yaml_scalar(valor) == esperado


# ---------------------------------------------------------------------------
# settings/strategy_source.py: el fichero
# ---------------------------------------------------------------------------


class TestStrategySource:
    def test_lee_el_yaml_del_temporal(self, yaml_tmp):
        cfg = strategy_source.get_config()
        assert cfg["min_score"] == 59.5
        assert cfg["magic"] == 8882026
        assert cfg["symbols_allow"] == ["EURUSD"]

    def test_los_accesos_nombrados_leen_de_la_misma_carga(self, yaml_tmp):
        assert strategy_source.get_data_sources() == st.DEFAULTS["data_sources"]
        assert strategy_source.get_watcher_config()["symbols"] == [
            {"symbol": "EURUSD", "timeframe": "M15"}
        ]
        assert strategy_source.get_agent_topics() == st.DEFAULTS["agent_topics"]

    def test_yaml_inexistente_dice_donde_buscalo(self, tmp_path, monkeypatch):
        """El error tiene que nombrar el archivo: "no hay configuración" sin saber
        dónde se buscó es la mitad del trabajo de diagnóstico."""
        monkeypatch.setattr(strategy_source, "STRATEGY_PATH", str(tmp_path / "nope.yaml"))
        strategy_source.invalidate()
        with pytest.raises(st.StrategyConfigError) as exc:
            strategy_source.get_config()
        assert "nope.yaml" in str(exc.value)

    def test_editar_el_fichero_a_mano_cambia_lo_que_se_sirve(self, yaml_tmp):
        """La caché se invalida por firma del archivo.

        REF cacheaba hasta el siguiente guardado, así que editar `strategy.yaml`
        no cambiaba nada hasta reiniciar: la UI seguía enseñando el valor viejo y
        guardaba encima. Aquí mtime + tamaño bastan.
        """
        assert strategy_source.get_config()["min_score"] == 59.5
        yaml_tmp.write_text(
            yaml_tmp.read_text(encoding="utf-8").replace("min_score: 59.5", "min_score: 71"),
            encoding="utf-8",
        )
        assert strategy_source.get_config()["min_score"] == 71

    def test_guardar_reescribe_una_sola_linea(self, yaml_tmp):
        antes = yaml_tmp.read_text(encoding="utf-8")
        cfg = strategy_source.get_config()
        cfg["magic"] = 1234
        devuelta = strategy_source.save(cfg)

        despues = yaml_tmp.read_text(encoding="utf-8")
        assert "  magic: 1234" in despues
        assert "# por operación" in despues, "el comentario de otra línea debe seguir"
        assert devuelta["magic"] == 1234
        # Solo cambia la línea editada: el resto del archivo es idéntico.
        assert len(antes.splitlines()) == len(despues.splitlines())

    def test_guardar_sin_cambios_no_toca_el_archivo(self, yaml_tmp):
        """Guardar lo que ya estaba es un no-op: `os.replace` reescribe el archivo
        entero y cambia su mtime, lo que invalidaría cachés de otros procesos sin
        que haya cambiado ninguna regla."""
        antes = yaml_tmp.stat().st_mtime_ns
        cfg = strategy_source.get_config()
        strategy_source.save(cfg)
        assert yaml_tmp.stat().st_mtime_ns == antes

    def test_guardar_las_pesadas_usa_el_documento(self, yaml_tmp):
        """`risk_weights` no cabe en un escalar en línea: se regenera el documento.

        Es el precio conocido de REF y no se ha mejorado: perder los comentarios del YAML
        al cambiar los pesos es aceptable; cambiar de aplanado a mitad sería no.
        """
        cfg = strategy_source.get_config()
        cfg["risk_weights"] = json.dumps({"cot": 5.0, "cvd_of": 20.0, "smc": 55.0,
                                          "killzone": 0.0, "smr_dxy": 20.0})
        devuelta = strategy_source.save(cfg)
        doc = st.load_doc(yaml_tmp.read_text(encoding="utf-8"))
        assert doc["score"]["weights"]["smc"] == 55.0
        assert json.loads(devuelta["risk_weights"])["cot"] == 5.0

    def test_claves_desconocidas_no_llegan_al_fichero(self, yaml_tmp):
        antes = yaml_tmp.read_text(encoding="utf-8")
        strategy_source.save({"clave_inventada": 1, "magic": 7})
        despues = yaml_tmp.read_text(encoding="utf-8")
        assert "clave_inventada" not in despues
        assert "  magic: 7" in despues

    def test_un_yaml_roto_no_deja_la_cache_servida(self, yaml_tmp):
        """Un guardado que deja el YAML ilegible tiene que tumbar la lectura.

        Servir la config cacheada "para no fallar" es lo contrario: el sistema
        seguiría operando con reglas que ya no están en el disco.
        """
        cfg = strategy_source.get_config()
        strategy_source.save(cfg)
        strategy_source.invalidate()
        yaml_tmp.write_text("risk: [\n", encoding="utf-8")
        with pytest.raises(st.StrategyConfigError):
            strategy_source.get_config()

    def test_un_valor_fuera_de_rango_no_llega_al_fichero(self, yaml_tmp):
        """Se valida antes de escribir, no después.

        REF escribía y luego recargaba: un `risk_pct: 500` dejaba el `strategy.yaml`
        —el fichero que decide si se opera— ilegible para el siguiente arranque, y
        devolvía un 400 como si el problema fuera de la petición.
        """
        antes = yaml_tmp.read_text(encoding="utf-8")
        with pytest.raises(st.StrategyConfigError):
            strategy_source.save({"risk_pct": 500})
        assert yaml_tmp.read_text(encoding="utf-8") == antes

    def test_la_escritura_deja_el_archivo_en_lf(self, yaml_tmp):
        """En Windows, `open(..., "w")` traduce los LF a CRLF: el YAML se iba
        convirtiendo de formato con cada guardado, y un diff de una línea cualquiera
        arrastraba el archivo entero."""
        assert b"\r\n" in yaml_tmp.read_bytes(), "el fixture lo escribe con CRLF a propósito"
        cfg = strategy_source.get_config()
        cfg["magic"] = 999
        strategy_source.save(cfg)
        crudo = yaml_tmp.read_bytes()
        assert b"\r\n" not in crudo
        assert yaml_tmp.with_suffix(".yaml.tmp").exists() is False