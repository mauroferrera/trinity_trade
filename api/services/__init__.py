"""Servicios: la lógica que orquesta puertos y NO sabe de HTTP.

Un endpoint de `api/routes/` hace tres cosas y ninguna más: leer la petición,
llamar a un servicio, y decidir el código de respuesta. Lo que no hace es
calcular. Por eso los cálculos que en REF estaban dentro de los handlers
—el score, el snapshot, la decisión de entrada, el estado de riesgo del día—
viven aquí, con `core/` para lo puro y los puertos para lo que habla con el mundo.

`services/` es I/O: puede abrir MT5, leer la base y escribir ficheros. Lo que no
puede es decidir la ESTRATEGIA, que es de `core/` (regla 19 de
`AGENT_GUIDELINES.md`: el agente explica, el motor decide).

Módulos:

- `mt5_market`: `MarketPort` sobre el adaptador de MT5. Es la implementación que
  la Fase 5 dejó declarada y sin cablear.
- `broker_clock`: la hora del servidor del broker y el día de trading.
- `macro_news`: `NewsPort` sobre `macro_ingestor`.
- `orderflow`: el acumulador de cinta (CVD, deltas, absorción) y su estado.
"""
