"""Capa de explicabilidad e interpretacion (LLM).

Responsabilidad: traducir datos a lenguaje natural y auditoria post-mortem.
NUNCA calcula entradas, SL o TP en tiempo de ejecucion \u2014 eso es territorio de
`core/risk_engine.py` con logica determinista.
"""