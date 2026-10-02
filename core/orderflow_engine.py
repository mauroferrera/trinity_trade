"""Acumulación de order flow: CVD, spikes Z-score y absorción.

Vive en `core/` y no en la API porque es DOMINIO, no infraestructura: decide qué
es un spike, qué es absorción y cuándo una zona cuenta como episodio nuevo. Esas
tres decisiones son las que se calibraron contra la cinta real, así que el código
que las toma tiene que poder ejecutarse sin broker, sin licencia y sin red.

Regla dura de este módulo: NO importa `adapters`, `api`, `agent`, MetaTrader5 ni
Databento. Recibe ticks ya normalizados y devuelve dicts. Eso es lo que permite
que los tests corran contra las fixtures JSONL sin levantar nada.

Diferencias respecto a la referencia (`app.py`):

- El símbolo se INYECTA en el constructor. En la referencia venía de una constante
  global (`OF_SYMBOL`), lo que fijaba el motor a un único instrumento; aquí el
  default sigue siendo ese símbolo pero cualquier feed puede pasar el suyo.
- El formateo de timestamps de alerta se INECTA (`fmt_ts`). La referencia usaba
  `tclock.fmt_utc`, que arrastra `strategy` y MetaTrader5 al núcleo; aquí el
  default es un formateo UTC local sin dependencias.
- El CVD sintético por tick volume (`candle_delta`, `build_cvd_series`) y la
  selección de fuente (`pick_cvd_source`) se traen aquí como funciones puras. En
  la referencia vivían en `cvd_service.py`, que además fetchaba de MT5; esa parte
  de I/O no se porta a `core/` porque pertenece al adaptador.

Convención de lado: `side` "A" = agresor comprador (agresa el ask, es decir,
compra), "B" = agresor vendedor. Es la convención de Databento y la que espera el
resto del núcleo; el adaptador es quien traduce el lado del proveedor a esta.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Optional

from core.orderflow_config import (
    OF_ABSORB_DELTA_RATIO,
    OF_ABSORB_EPISODE_GAP_S,
    OF_ABSORB_RANGE_RATIO,
    OF_ABSORB_TRADES,
    OF_ABSORB_VOL_MIN,
    OF_EMA_WINDOW,
    OF_SPIKE_EPISODE_GAP_S,
    OF_SYMBOL,
    OF_ZSCORE_MIN_SIZE,
    OF_ZSCORE_THRESHOLD,
)

__all__ = [
    "OrderFlowEngine",
    "candle_delta",
    "build_cvd_series",
    "pick_cvd_source",
    "DELTA_RATIO",
]

# Flujo asumido de tick_volume por vela direccional en el CVD sintético.
DELTA_RATIO = 0.6


def _default_fmt_ts(ts: Any, pattern: str = "%H:%M:%S") -> str:
    """Formatea un epoch UTC como HH:MM:SS.

    Réplica local de `tclock.fmt_utc(ts, pattern, "")`. No se importa `tclock` a
    propósito: ese módulo arrastra `strategy` y MetaTrader5, y el núcleo no puede
    depender de ninguno de los dos. El default es inyectable por si el adaptador
    quiere su propia convención de zona.
    """
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return dt.strftime(pattern)


class OrderFlowEngine:
    """Acumula métricas de order flow (CVD, spikes Z-score, absorción) thread-safe.

    Alimentarla es `add_trade`; leerla es `snapshot`. Todo el estado mutable vive
    detrás de un lock porque el feed empuja ticks desde un hilo y la API los lee
    desde otro: sin el lock, `snapshot` puede observar un CVD a medio actualizar y
    devolver una serie con un salto imposible.
    """

    def __init__(
        self,
        symbol: str = OF_SYMBOL,
        fmt_ts: Callable[[Any, str], str] = _default_fmt_ts,
    ):
        self._lock = threading.Lock()
        self.symbol = symbol
        self._fmt_ts = fmt_ts
        self._volumes = deque(maxlen=200)
        self._ema = None
        self._ema_var = 0.0
        self.recent_trades = deque(maxlen=300)
        self.cvd = 0.0
        self.delta = 0.0
        self.buy_vol = 0.0
        self.sell_vol = 0.0
        self.total_vol = 0.0
        self.spike_count = 0
        self.spike_episodes = 0
        self._last_spike_ts = None
        self.absorb_episodes = 0
        self.alerts = deque(maxlen=60)
        self.last_price = None
        self.absorb_bins = {}
        self.zscore_threshold = OF_ZSCORE_THRESHOLD
        self.zscore_min_size = OF_ZSCORE_MIN_SIZE
        self.ema_window = OF_EMA_WINDOW
        self.absorb_trades = OF_ABSORB_TRADES
        self.absorb_vol_min = OF_ABSORB_VOL_MIN
        self.absorb_range_ratio = OF_ABSORB_RANGE_RATIO
        self.absorb_delta_ratio = OF_ABSORB_DELTA_RATIO
        self.cvd_history = deque(maxlen=3000)
        self._cvd_bucket = None
        # Métricas de vivosidad: momento y caudal del último trade.
        self.last_trade_wall = None  # time.time() de la última cinta recibida
        self.last_trade_ts = None    # ts_event (s) del último trade
        self.msg_count = 0           # trades procesados desde el último reset
        self._rate_window = deque(maxlen=3000)  # wall-times para msgs/s

    # --- Configuración ---------------------------------------------------------

    def update_settings(self, zscore_threshold=None, absorb_trades=None,
                        zscore_min_size=None, ema_window=None,
                        absorb_vol_min=None, absorb_range_ratio=None,
                        absorb_delta_ratio=None):
        with self._lock:
            if zscore_threshold is not None:
                self.zscore_threshold = round(max(0.1, float(zscore_threshold)), 2)
            if absorb_trades is not None:
                self.absorb_trades = int(max(20, min(absorb_trades, 300)))
            if zscore_min_size is not None:
                self.zscore_min_size = int(max(0, min(zscore_min_size, 1000)))
            if ema_window is not None:
                self.ema_window = int(max(2, min(ema_window, 5000)))
            if absorb_vol_min is not None:
                self.absorb_vol_min = float(absorb_vol_min)
            if absorb_range_ratio is not None:
                self.absorb_range_ratio = float(absorb_range_ratio)
            if absorb_delta_ratio is not None:
                self.absorb_delta_ratio = float(absorb_delta_ratio)
            return self.settings_locked()

    def settings_locked(self):
        return {
            "zscore_threshold": self.zscore_threshold,
            "zscore_min_size": self.zscore_min_size,
            "ema_window": self.ema_window,
            "absorb_trades": self.absorb_trades,
            "absorb_vol_min": self.absorb_vol_min,
            "absorb_range_ratio": self.absorb_range_ratio,
            "absorb_delta_ratio": self.absorb_delta_ratio,
        }

    def settings(self):
        with self._lock:
            return self.settings_locked()

    def reset(self):
        """Reinicia el estado del engine a cero (usado en cambios Live <-> Mock)."""
        with self._lock:
            self._volumes.clear()
            self._ema = None
            self._ema_var = 0.0
            self.recent_trades.clear()
            self.cvd_history.clear()
            self.alerts.clear()
            self.absorb_bins = {}
            self.cvd = 0.0
            self.delta = 0.0
            self.buy_vol = 0.0
            self.sell_vol = 0.0
            self.total_vol = 0.0
            self.spike_count = 0
            self.spike_episodes = 0
            self._last_spike_ts = None
            self.absorb_episodes = 0
            self.last_price = None
            self._cvd_bucket = None
            self.last_trade_wall = None
            self.last_trade_ts = None
            self.msg_count = 0
            self._rate_window.clear()

    # --- Interno --------------------------------------------------------------

    def _update_zscore(self, size):
        if self._ema is None:
            self._ema = float(size)
            self._ema_var = 0.0
            return 0.0
        alpha = 2.0 / (self.ema_window + 1)

        prev_ema = self._ema
        self._ema = alpha * size + (1 - alpha) * prev_ema
        var = (1 - alpha) * (self._ema_var + alpha * (size - prev_ema) ** 2)
        self._ema_var = var
        std = math.sqrt(max(var, 1e-12))
        if std == 0:
            return 0.0
        return (size - self._ema) / std

    def _detect_absorption(self, now_ts):
        trades = list(self.recent_trades)
        win = self.absorb_trades
        if len(trades) < 30 or len(trades) < win:
            return None
        window = trades[-win:]
        prices = [t["price"] for t in window]
        net_delta = sum((1 if t["side"] == "A" else -1) * t["size"] for t in window)
        buy = sum(t["size"] for t in window if t["side"] == "A")
        sell = sum(t["size"] for t in window if t["side"] == "B")
        high = max(prices)
        low = min(prices)
        mid = (high + low) / 2.0
        total = buy + sell
        if mid == 0:
            return None
        range_ratio = (high - low) / mid
        delta_ratio = abs(net_delta) / max(total, 1e-9)
        if (total >= self.absorb_vol_min and range_ratio <= self.absorb_range_ratio
                and delta_ratio <= self.absorb_delta_ratio):
            direction = "compra" if net_delta >= 0 else "venta"
            level = (high + low) / 2.0
            key = round(level, 5)
            # Level gating: la alerta solo se emite al ENTRAR en la zona; se
            # suprime mientras la condición siga activa en este nivel (o en uno
            # cercano dentro de la tolerancia de rango) con re-confirmación
            # dentro del gap de episodio. El bin sí sigue acumulando volumen
            # consolidado en cada detección, aunque se suprima la alerta.
            now_active = lambda b: abs(b["level"] - level) <= self.absorb_range_ratio * level \
                and (now_ts - b["last_ts"]) <= OF_ABSORB_EPISODE_GAP_S
            bin_state = self.absorb_bins.get(key)
            episode_start = not (bin_state is not None and now_active(bin_state))
            if episode_start:
                episode_start = not any(now_active(b) for b in self.absorb_bins.values())
            if bin_state is None:
                bin_state = self.absorb_bins[key] = {
                    "level": float(key),
                    "volume": 0.0,
                    "net_delta": 0.0,
                    "direction": direction,
                    "first_ts": now_ts,
                    "last_ts": now_ts,
                }
            bin_state.update(volume=total, net_delta=net_delta,
                             direction=direction, last_ts=now_ts)
            if episode_start:
                self.absorb_episodes += 1
            return {
                "level": level,
                "volume": total,
                "net_delta": net_delta,
                "buy_vol": buy,
                "sell_vol": sell,
                "range": high - low,
                "direction": direction,
                "episode_start": episode_start,
            }
        return None

    # --- Entrada ---------------------------------------------------------------

    def add_trade(self, price, size, side, ts):
        """Procesa un tick normalizado y devuelve el payload del evento.

        `side` admite el string "A"/"B" o cualquier enum que exponga `.value`
        con esos valores (es como los entrega el adaptador de Databento). Un lado
        desconocido devuelve None en vez de inventar un delta.
        """
        side_str = side.value if hasattr(side, "value") else side
        if side_str not in ("A", "B"):
            return None
        delta = size if side_str == "A" else -size
        with self._lock:
            self._volumes.append(size)
            zscore = self._update_zscore(size)
            self.cvd += delta
            self.delta = delta
            self.buy_vol += size if side_str == "A" else 0
            self.sell_vol += size if side_str == "B" else 0
            self.total_vol += size
            self.last_price = price

            trade = {
                "ts": ts,
                "price": price,
                "size": size,
                "side": "A" if side_str == "A" else "B",
            }
            self.recent_trades.append(trade)

            now_wall = time.time()
            self.last_trade_wall = now_wall
            self.last_trade_ts = ts
            self.msg_count += 1
            self._rate_window.append(now_wall)

            bucket = int(ts)
            if self._cvd_bucket is not None and bucket > self._cvd_bucket:
                last = self.cvd_history[-1] if self.cvd_history else None
                if last is None or last["time"] != self._cvd_bucket:
                    self.cvd_history.append({"time": self._cvd_bucket, "value": round(self.cvd, 2)})
            self._cvd_bucket = bucket

            is_spike = bool(size >= self.zscore_min_size and zscore >= self.zscore_threshold)
            new_episode = False
            if is_spike:
                self.spike_count += 1
                if self._last_spike_ts is None or (ts - self._last_spike_ts) > OF_SPIKE_EPISODE_GAP_S:
                    self.spike_episodes += 1
                    new_episode = True
                self._last_spike_ts = ts

            absorption = self._detect_absorption(ts)

            payload = {
                "event": "trade",
                "symbol": self.symbol,
                "ts": ts,
                "price": price,
                "size": size,
                "side": "A" if side_str == "A" else "B",
                "delta": delta,
                "cvd": round(self.cvd, 2),
                "zscore": round(zscore, 2),
                "is_spike": is_spike,
                "total_vol": round(self.total_vol, 2),
                "buy_vol": round(self.buy_vol, 2),
                "sell_vol": round(self.sell_vol, 2),
            }
            if is_spike and new_episode:
                payload["alert"] = f"Spike institucional detectado (Z {zscore:.2f}, {size} lotes)"
                self.alerts.append({
                    # UTC: la serie de este feed son epochs UTC, y estos alerts
                    # se leen junto a las velas del gráfico, que ya son UTC.
                    "ts": self._fmt_ts(ts, "%H:%M:%S"),
                    "price": price,
                    "size": size,
                    "zscore": round(zscore, 2),
                    "side": "A" if side_str == "A" else "B",
                    "alert": payload["alert"],
                    "kind": "spike",
                })
            if absorption is not None:
                if absorption.get("episode_start"):
                    payload["absorption"] = absorption
                    payload["alert"] = f"Absorción ({absorption['direction']}) en {absorption['level']:.5f}"
                    self.alerts.append({
                        "ts": self._fmt_ts(ts, "%H:%M:%S"),
                        **absorption,
                        "alert": payload["alert"],
                    })
            return payload

    # --- Salida ----------------------------------------------------------------

    def snapshot(self):
        with self._lock:
            now_wall = time.time()
            recent = sum(1 for t in self._rate_window if now_wall - t <= 60.0)
            return {
                "symbol": self.symbol,
                "cvd": round(self.cvd, 2),
                "delta": round(self.delta, 2),
                "total_vol": round(self.total_vol, 2),
                "buy_vol": round(self.buy_vol, 2),
                "sell_vol": round(self.sell_vol, 2),
                "last_price": self.last_price,
                "spike_count": self.spike_count,
                "spike_episodes": self.spike_episodes,
                "absorb_episodes": self.absorb_episodes,
                "absorb_bins": sorted(
                    ({"key": k, **v} for k, v in self.absorb_bins.items()),
                    key=lambda b: -b["volume"],
                ),
                "alerts": list(self.alerts),
                "ema": round(self._ema, 2) if self._ema is not None else None,
                "settings": self.settings_locked(),
                "msg_count": self.msg_count,
                "msgs_per_sec": round(recent / 60.0, 2) if recent else 0.0,
                "last_trade_wall": self.last_trade_wall,
            }

    def cvd_series(self, since=None):
        with self._lock:
            if since is None:
                return list(self.cvd_history)
            cutoff = int(since)
            return [p for p in self.cvd_history if p["time"] >= cutoff]

    def msgs_per_sec(self):
        """Trades por segundo medidos en una ventana deslizante de ~60 s."""
        with self._lock:
            if not self._rate_window:
                return 0.0
            now = time.time()
            n = sum(1 for t in self._rate_window if now - t <= 60.0)
            return round(n / 60.0, 2)


# --- CVD sintético (puro) -----------------------------------------------------


def candle_delta(open_: float, close: float, volume: float) -> float:
    """Delta sintético de una vela según su cuerpo y tick volume.

    El modelo es una APROXIMACIÓN declarada, no una medición: sin cinta MBO real
    no hay delta por nivel, así que se asume que una vela direccional mueve
    DELTA_RATIO de su tick volume. Lo que importa es que sea reproducible y que no
    afirme ser más de lo que es; la etiqueta "synthetic" del selector lo dice.
    """
    volume = float(volume)
    if volume <= 0:
        return 0.0
    if close > open_:
        return volume * DELTA_RATIO
    if close < open_:
        return -volume * DELTA_RATIO
    return 0.0  # doji / neutral


def build_cvd_series(candles: Iterable[dict]) -> List[dict]:
    """Acumula `candle_delta` vela a vela y devuelve la serie {time, value}.

    Espera velas OHLC dicts (time/open/close/volume), no filas crudas del broker:
    quien traduce el shape es el adaptador. Los `time` de salida son los de
    entrada, sin normalizar, para que la serie encaje con las velas que ya ve el
    gráfico sin una segunda convención de tiempo dentro del núcleo.
    """
    cvd = 0.0
    points: List[dict] = []
    for c in candles:
        cvd += candle_delta(c["open"], c["close"], c.get("volume") or 0.0)
        points.append({"time": c["time"], "value": round(cvd, 2)})
    return points


def pick_cvd_source(live_points, synthetic_points, min_live_points=5,
                    live_label="live", synthetic_label="synthetic",
                    live_detail="", synthetic_detail=""):
    """Elige la fuente real del CVD para el snapshot del gráfico.

    Pura y testeable. Devuelve (points, source, warning).

    - Si hay serie live suficiente (>= min_live_points) gana la live.
    - Si la live es insuficiente/ausente pero hay sintética, devuelve la sintética
      con warning, para no mentir en el label.
    - Si no hay ninguna, devuelve None y un warning que explica el hueco.

    Las etiquetas se INYECTAN en lugar de hardcodearse. La referencia devolvía
    "live (Databento 6E)" / "synthetic (tick volume MT5)", literales que mentían
    en cuanto el feed no era Databento o el símbolo no era 6E; con DI, el llamador
    dice la verdad porque la sabe.
    """
    live = list(live_points) if live_points else []
    syn = list(synthetic_points) if synthetic_points else []

    if len(live) >= min_live_points:
        label = live_label if not live_detail else f"{live_label} ({live_detail})"
        return live, label, None
    if syn:
        if live:
            warning = (
                f"feed live conectado pero sin suficientes puntos recientes "
                f"({len(live)}/{min_live_points}), se usa serie sintética."
            )
        else:
            warning = None
        label = synthetic_label if not synthetic_detail else f"{synthetic_label} ({synthetic_detail})"
        return syn, label, warning
    return None, None, "sin datos de CVD disponibles"