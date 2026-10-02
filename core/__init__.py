"""Nucleo determinista y agnostico al mercado.

Regla dura: este paquete NO importa `adapters`, `api`, `agent`, MT5 ni ningun
proveedor de datos. Recibe datos normalizados (OHLC/Ticks/Depth) y devuelve
resultados deterministas.
"""