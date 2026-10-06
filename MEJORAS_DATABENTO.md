# MEJORAS_DATABENTO.md - Calibración del Order Flow contra cinta real

Histórico de **por qué** `core/orderflow_config.py` tiene los valores que tiene,
y de lo que costó conseguir la cinta con la que se midieron. El módulo cita este
archivo en su docstring; si cambia un umbral, cambia esto, y viceversa.

Última recalibración: **2026-10-06**, sobre `tests/fixtures/orderflow_6e_real.jsonl`.

---

## 1. Qué se descargó y dónde vive

| | |
|---|---|
| Dataset | `GLBX.MDP3`, schema `trades` |
| Contrato | **`6EZ6`** (diciembre 2026) — ver §2 |
| Sesión | 2026-10-05 completa → 51.118 trades, Parquet 1.341,6 KiB |
| Coste | 0,0640 USD (más 0,0003 USD de una descarga de control) |
| Cliente | `databento==0.85.0`, `pyarrow==25.0.1`, `pandas==3.0.5` |
| Raíz | `TRINITY_DATA_ROOT` → `C:\Users\fmaur\Desktop\trinity_data\databento\raw\` |
| API key | `.env` en la raíz del repo, gitignored (`.gitignore:9`) |

Todo lo pesado vive **fuera del repo y fuera de OneDrive** (D-064, `core/paths.py`).
Cada Parquet lleva al lado un `*.parquet.meta.json` con `sha256`, filas, coste y
ventana: sin ese sidecar una descarga no es reproducible ni verificable.

Comandos:

```bash
python research/fetch_databento.py --start 2026-10-05 --end 2026-10-06 --symbol 6EZ6 --jsonl <ruta>
python research/build_real_fixture.py     # regenera fixture + sidecar desde el Parquet
```

`requirements.txt` clava `pandas==2.2.3` y `pytest==8.3.4` mientras instalados son
`3.0.5` y `9.1.1`: discrepancia **preexistente**, documentada y no tocada.

---

## 2. El roll silencioso: `6E.c.0` no es el mercado (D-066)

`OF_SYMBOL = "6E.c.0"` significa *"próximo a expirar"* y **se recalcula solo**.
Al llegar el roll dejó de apuntar al contrato que opera el mercado:

```
6E.c.0 -> instrument_id 42001229 -> 6EV6  (octubre)  246 trades en todo el día
6E.c.1 -> instrument_id 42823529 -> 6EX6  (noviembre)
6E.c.2 -> instrument_id   5510   -> 6EZ6  (diciembre) 51.118 trades en todo el día
```

**208× de diferencia de liquidez.** La cinta de `6E.c.0` el día completo:

| | `6EV6` (`6E.c.0`) | `6EZ6` (`6E.c.2`) |
|---|---|---|
| trades/día | **246** | **51.118** |
| media de size | 2,276 | 4,263 |
| mediana de size | 1 | 2 |
| precios distintos | 100 | 191 |
| prints sin agresor (`side=N`) | 87 (**35 %**) | 698 (1,4 %) |

La calibración original tenía pinta de haberse hecho sobre el contrato líquido y
de haberse podado en silencio al hacer roll. `6E.c.2` resolvería al bueno, pero
el offset numérico es frágil (depende de que el líquido siga siendo el tercero).

**Decisión (D-066):** `OF_SYMBOL = "6E.c.0"` **se conserva como identidad** —
`normalize_symbol("6E.c.0") == "6E"` lo afirma `tests/unit/test_base_adapter.py`
y lo espera el frontend —, y la **descarga y la calibración** van explícitamente
contra `6EZ6`. Identidad y contrato negociado son dos cosas distintas y el código
ya no las mezcla.

---

## 3. La calibración original y su reconciliación (D-067)

El docstring original citaba, sobre una misma sesión:

- ~20,5k trades, media de size ~4,4, mediana ~2
- **620** prints a Z ≥ 2,5 · **42** a Z ≥ 4,5 · **~10** zonas
- ventana de 300 trades acumulando ~1.300 contratos · ~4 zonas de absorción
- y lo llamaba **"4 h NY"**

Esas cifras **no se reproducen en la cinta de 2026-10-05**, y la causa se ve
mirando cuánto rinde cada ventana:

- Ninguna ventana de **4 h** pasa de **14.8k trades** (máximo de la jornada), así
  que "4 h" y "20.5k trades" son incompatibles entre sí **ya en el original**.
- A día completo hay **1511** prints a Z ≥ 2,5 y **116** a Z ≥ 4,5. Escalando a
  20,5k trades (≈41 % del día) eso da **≈616 / 47** — muy cerca de **620 / 42**.
- Confirmado empíricamente: las ventanas de **8 h con 20-23k trades** producen
  **Z ≥ 2.5 = 627-653**.

Es decir: la sesión original era de **~8 h, no 4 h**, y era **otro día** (no
tenemos su cinta). Las tres cifras eran internamente coherentes; lo que no era
verdad era el rótulo de "4 h".

**Decisión (D-067):** el fixture se fija en **8 h de NY, 10:00-18:00 UTC del
2026-10-05** — es la ventana que mejor reproduce Z ≥ 2.5 (631) y cae dentro del
horario de Nueva York —, y **la prosa se recalibra a lo que ese fixture mide**.
Los valores de los umbrales **no cambian**: solo cambian los números que los
describen. El historial (620/42/~10) se conserva aquí, en este párrafo, porque
borrarlo sin más haría que nadie supiera que alguna vez existió.

---

## 4. Parámetros vigentes y lo que miden sobre el fixture

Ventana: 8 h NY, 10:00-18:00 UTC, 2026-10-05, `6EZ6` → **21.926 trades**.

**Cinta**

| métrica | valor |
|---|---|
| media de size | 4,156 |
| mediana de size | **2** |
| precios distintos | 64 |
| prints con `size >= 75` | **16** (máximo 230) |
| volumen en 300 trades | mín. **837** · media **1.249** · máx. 2.354 |

**Motor** (`OF_EMA_WINDOW = 50`, techo `zscore_ceiling(50) = 4,9497`)

| umbral | resultado |
|---|---|
| `OF_ZSCORE_THRESHOLD` = **4,5043** (= 0,91 × techo) | — |
| Z ≥ 2,5 | **631** prints |
| Z ≥ 4,5 | **61** prints |
| Z ≥ 4,5 **y** `size >= 75` | **8** spikes → **8** zonas |
| Z ≥ 5,0 | **0** prints (el techo lo hace inalcanzable) |
| absorción (`range_ratio = 0,0002`) | **1** zona |
| CVD final | **−2.420** (44.357 compra / 46.777 venta) |

Lectura: sin el gate de tamaño, 61 prints a Z ≥ 4.5 serían 61 alertas de
ruido microestructural; **el gate es lo que las convierte en 8 episodios**. Y el
`vol_min = 40` no llega a ser el gate en ninguna parte: la ventana más flaca de
toda la sesión acumula 837, veinte veces eso, así que en la práctica filtran el
rango y el delta.

El motivo de `OF_ZSCORE_THRESHOLD_FRAC = 0.91` está en D-065: `_update_zscore`
actualiza la EMA **antes** de puntuar, con lo que la varianza EWMA de la muestra
es siempre la del propio tamaño y el Z queda acotado por `sqrt((w-1)/2)`. Con
`w = 50` el techo es 4,9497 y un 4,5 fijo entra; con `w = 20` el techo baja a
3,082 y ese mismo 4,5 queda **por encima**: el detector se apaga sin error. El
umbral como fracción es lo que hace visible esa dependencia.

---

## 5. Regenerar el fixture

```bash
python research/build_real_fixture.py
```

Lee el Parquet ya descargado (no vuelve a pagar la API), corta la ventana,
filtra `side` fuera de `A`/`B`, escribe el `.jsonl` y su sidecar con `sha256_lf`
(sha256 sobre bytes con LF, para que no dependa del checkout: `.gitattributes`
marca `* text=auto` y el working tree de Windows puede tener CRLF).

`tests/unit/test_orderflow_real.py` fija ese sha y vuelve a medir el motor, de
modo que **o el fixture es el que dice el sidecar, o la prosa de
`orderflow_config.py` es la que dice el test**. Si algo falla, se regenera; no
se parchea el número a mano.

---

## 6. Artefactos y rarezas de la API de Databento 0.85

Conocidos y ya manejados en `research/fetch_databento.py`:

- **`ts_event` es `datetime64[ns, UTC]`, no un entero.** `float()` sobre él lanza
  `TypeError`; hay que leer `.value` (ns). Es el error que aparece si se asume
  que el valor ya es numérico.
- **`side` de un trade = lado del AGRESOR** (`databento_dbn.Side`: *"the side of
  the aggressor for trades"*). `A` = agresor compró al ask, `B` = vendió al bid,
  `N` = sin agresor identificado. Coincide con la convención que ya asumía
  `OrderFlowEngine`, así que no hay traducción que hacer, solo descartar `N`
  (35 % en el contrato muerto, 1,4 % en el líquido).
- **Simbología inclusiva y con horizonte**: `start_date == end_date` → `422
  data_date_range_start_on_or_after_end`; fechas futuras → `422
  dataset_unavailable_range` con el mensaje *"requires a subscription and/or
  license"*, que **no menciona fechas** y parece un problema de cuenta cuando es
  de calendario. El horizonte de datos es *now − ~10 min*.
- **Combinaciones de symbology**: `stype_in=continuous` solo admite
  `stype_out = instrument_id | parent | continuous` para `GLBX.MDP3`; `raw_symbol`
  con `continuous` da `422 symbology_invalid_request`. Para mapear
  `instrument_id → código de contrato` hace falta una segunda llamada
  (`instrument_id → raw_symbol`).
- **`DBNStore.from_file` no puede releer el Parquet que nosotros escribimos**
  (`BentoError: Could not determine compression format`); hay que iterar el
  `store` que devuelve `get_range` en memoria o leerlo con `pandas.read_parquet`.
- **`db.Historical()`, no `db.History`** (la clave la lee de `DATABENTO_API_KEY`).
- **La consola de Windows escribe cp1252 y lee cp850**: acentos rotos en
  stdout/stderr de los scripts. `fix_console_encoding()` lo arregla solo si hay
  `isatty()`, para no tocar la salida de CI.
- **`python-dotenv` estaba en requirements y `load_dotenv` no se llamaba en
  ningún sitio.** Ahora lo llama `research/fetch_databento.py`; `core/` no puede
  importarlo (`tests/unit/test_core_purity.py` lo prohibe).

---

## 7. Pendiente

- **Rotar la clave de Databento.** Se pegó en texto plano en una conversación
  antes de pasarla a `.env`. La rotación es de la cuenta, no del repo.
- `data_sources.orderflow: true` y cablear `adapters/forex/databento_cme.py` como
  feed en vivo: fuera de alcance de esta calibración.
- Redes neuronales: postergadas. El motor determinista
  (`smc_engine.py` + `orderflow_engine.py`) sigue siendo el baseline.
