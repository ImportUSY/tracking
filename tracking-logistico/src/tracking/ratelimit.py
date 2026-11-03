"""Limitador de frecuencia en memoria (ventana deslizante), seguro para hilos.

Suficiente para una sola instancia. Con varias réplicas, colocar el límite en el proxy
(nginx/Caddy) o sustituir esta clase por una implementación sobre Redis.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_gc = time.monotonic()

    def hit(self, key: str) -> bool:
        """Registra un intento; devuelve False si excede el límite."""
        now = time.monotonic()
        with self._lock:
            self._gc(now)
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.max_events:
                return False
            q.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def _gc(self, now: float) -> None:
        if now - self._last_gc < 60:
            return
        self._last_gc = now
        stale = [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]
        for k in stale:
            del self._hits[k]
