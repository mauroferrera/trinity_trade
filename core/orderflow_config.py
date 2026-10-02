"""Fuente única de verdad de los parámetros del Order Flow (6E Databento).

`app.py` (engine) y `mock_feed.py` (feed sintético) importan ACÁ sus constantes:
cualquier recalibración toca un solo archivo. Histórico de calibración en
MEJORAS_DATABENTO.md (la cinta real 6E tiene mediana de size ~2 vs el ~12 que
asumía el mock; los umbrales se recalibraron contra
tests/fixtures/orderflow_6e_real.jsonl).
"""

OF_SYMBOL = "6E.c.0"
OF_DATASET = "GLBX.MDP3"

# Z-score sobre el tamaño de trade (EMA online de la cinta).
OF_EMA_WINDOW = 50
# Calibrado contra la cinta real (tests/fixtures/orderflow_6e_real.jsonl, 4 h NY):
#   - Z < 4.5 -> el ruido microestructural (median size 2) satura el contador
#     (620 spikes a Z>=2.5). A 4.5 quedan 42 prints aislados; combinado con el
#     gate de size (~75) se llega a ~10 zonas significativas por sesion.
#   - El Z-score de un solo print se satura por construccion en ~5 (la varianza
#     EWMA se actualiza con el mismo trade que puntua), por eso el salto
#     4.5 -> 42 trades y 5.0 -> 0.
OF_ZSCORE_THRESHOLD = 4.5
# Filtro de ruido microestructural + gate institucional: solo se FLAGGEAN
# spikes en trades con size >= este minimo (>=75 en la cinta real = agresion
# institucional; mediana real ~2, media ~4.4). La EMA sigue viendo TODA la cinta.
OF_ZSCORE_MIN_SIZE = 75

# Absorción: ventana (en trades), volumen mínimo acumulado y compresión de rango.
# Calibrado contra la cinta real: range_ratio=0.0002 (de 0.0004) colapsa los
# 200 bins a ~4 zonas; el vol_min deja de ser el gate vinculante en una sesion
# con >20k trades (cualquier ventana de 300 ya acumula ~1300 contratos).
OF_ABSORB_TRADES = 300
OF_ABSORB_VOL_MIN = 40
OF_ABSORB_RANGE_RATIO = 0.0002
OF_ABSORB_DELTA_RATIO = 0.25

# Coalescing de spikes en episodios/zones: un spike cuenta como "nuevo episodio"
# si el anterior ocurrió hace más de este gap (segundos).
OF_SPIKE_EPISODE_GAP_S = 5.0

# Coalescing de absorción en episodios/zones (level gating): al detectarse
# absorción en un nivel, se emite UNA alerta al entrar en la zona y se suprimen
# las siguientes mientras la condición siga activa en ese nivel (o uno cercano
# dentro de absorb_range_ratio * precio). El episodio expira pasados este gap
# de segundos sin re-confirmación en el nivel.
OF_ABSORB_EPISODE_GAP_S = 5.0