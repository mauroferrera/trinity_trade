"""Ingestores de datos macro y soft data por mercado.

Solo I/O: HTTP, webhooks y calendario económico. No contienen lógica de decisión.

El nombre del paquete es deliberado. Estos módulos NO son adaptadores de mercado:
un adaptador convierte cotizaciones en la forma normalizada y su fallo significa
"no se puede operar". Un ingestor macro lee contexto (posiciones de la CFTC, el
dólar, el calendario) y su fallo significa "una parte del score vale cero hoy".
Mezclarlos en `adapters/` haría que una caída del proxy de Forex Factory se
confundiera con una caída del bróker, y el motor de riesgo no podría distinguirlas.

El módulo compartido es `base_ingestor.py`. No se reexporta nada aquí: importar el
paquete no debe crear ninguna caché ni abrir ninguna conexión.

`registry.py` traduce los nombres de `config/asset_sources_map.yaml` a estos
módulos. Se importa a mano (`from macro_ingestor import registry`) por la misma
razón: quien importa tiene que decir de dónde viene el dato.
"""

__all__: list = []