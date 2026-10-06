"""Fuente única de verdad de los parámetros del Order Flow (6E Databento).

`app.py` (engine) y `mock_feed.py` (feed sintético) importan ACÁ sus constantes:
cualquier recalibración toca un solo archivo. Histórico de calibración en
MEJORAS_DATABENTO.md (la cinta real 6E tiene mediana de size ~2 vs el ~12 que
asumía el mock; los umbrales se recalibraron contra
tests/fixtures/orderflow_6e_real.jsonl).
"""

import math

OF_SYMBOL = "6E.c.0"
OF_DATASET = "GLBX.MDP3"


def zscore_ceiling(ema_window: int) -> float:
    """Techo matemático del Z-score de un solo print.

    El Z-score se puntúa DESPUÉS de actualizar la EMA con el mismo trade, así
    que la varianza EWMA de la muestra es siempre la del propio tamaño. Sustituyendo
    la recursión de `_update_zscore` y acotando:

        z = (1-a)·(s-m) / sqrt((1-a)·(v + a·(s-m)²))  ≤  sqrt((1-a)/a)

    con `a = 2/(w+1)` eso da `sqrt((w-1)/2)` exacto en la 2ª muestra y
    supremo para cualquier print posterior (el máximo se alcanza cuando el
    tamaño domina a la varianza acumulada).

    Consecuencia práctica: el umbral absoluto depende de la ventana. Con
    `w = 50` el techo es 4.9497; con `w = 20` es 3.082, y un umbral fijo de
    4.5 quedaría POR ENCIMA del techo: el detector se apaga sin error, en
    silencio. Por eso el umbral se expresa como fracción de este techo.
    """
    w = max(int(ema_window), 2)
    return math.sqrt((w - 1) / 2.0)


# Z-score sobre el tamaño de trade (EMA online de la cinta).
OF_EMA_WINDOW = 50
# Umbral como FRACCIÓN del techo, no como número fijo: sobrevive a cualquier
# cambio de ventana y hace visible la dependencia. 0.91 reproduce el umbral
# calibrado (4.5 sobre techo 4.9497 = 90.91%) a 4.5043, diferencia menor que
# la resolución a la que se reporta el Z.
OF_ZSCORE_THRESHOLD_FRAC = 0.91
OF_ZSCORE_THRESHOLD = OF_ZSCORE_THRESHOLD_FRAC * zscore_ceiling(OF_EMA_WINDOW)
# Calibrado contra la cinta real (tests/fixtures/orderflow_6e_real.jsonl:
# 6EZ6, 8 h de NY 10:00-18:00 UTC del 2026-10-05, 21926 trades):
#   - Z bajo -> el ruido microestructural (mediana de size 2) satura el
#     contador (631 prints a Z>=2.5). A ~4.5 quedan 61 prints aislados;
#     combinado con el gate de size (>=75 solo 16 veces en toda la sesion)
#     se llega a 8 zonas significativas.
#   - El Z-score de un solo print se satura por construccion en el techo
#     `zscore_ceiling`, por eso el salto 4.5 -> 61 trades y 5.0 -> 0.
#     La prosa original citaba 620/42/~10 sobre una sesion de 4 h que ya no
#     se reproduce; ver MEJORAS_DATABENTO.md para el historial.
# Filtro de ruido microestructural + gate institucional: solo se FLAGGEAN
# spikes en trades con size >= este minimo (>=75 en la cinta real = agresion
# institucional; mediana real 2, media 4.16). La EMA sigue viendo TODA la cinta.
OF_ZSCORE_MIN_SIZE = 75

# Absorción: ventana (en trades), volumen mínimo acumulado y compresión de rango.
# Calibrado contra la cinta real: range_ratio=0.0002 deja 1 sola zona en la
# sesión de 8 h (los bins colapsan); el vol_min deja de ser el gate vinculante
# en una sesión con >20k trades: cualquier ventana de 300 acumula de media
# 1249 contratos (mínimo 837, máximo 2354), muy por encima de 40.
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