"""Rate limiting.

La pauta de la EP3 pide rate limiting en el punto 2.2 y la EP2 no lo incluyo.
Se implementa como token bucket en proceso: cada tarea limita su propia cuota,
lo que basta porque el ALB reparte el trafico de forma pareja.

En produccion el limite de borde deberia estar tambien en el ALB o en WAF, para
que una tormenta no llegue siquiera a las tareas. Esto es la segunda capa.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """Limitador por cliente con relleno continuo.

    rate   : tokens que se reponen por segundo (solicitudes sostenidas)
    burst  : tamano maximo del balde (rafaga tolerada)
    """

    def __init__(self, rate: float, burst: float, clock=time.monotonic) -> None:
        if rate <= 0 or burst <= 0:
            raise ValueError("rate y burst deben ser positivos")
        self._rate = float(rate)
        self._burst = float(burst)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, client_id: str, cost: float = 1.0) -> bool:
        """True si la solicitud entra dentro de la cuota del cliente."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = _Bucket(tokens=self._burst, updated_at=now)
                self._buckets[client_id] = bucket

            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
            bucket.updated_at = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True
            return False

    def retry_after(self, client_id: str, cost: float = 1.0) -> float:
        """Segundos hasta que el cliente vuelva a tener cuota."""
        with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None or bucket.tokens >= cost:
                return 0.0
            missing = cost - bucket.tokens
            return missing / self._rate

    def reset(self, client_id: str | None = None) -> None:
        with self._lock:
            if client_id is None:
                self._buckets.clear()
            else:
                self._buckets.pop(client_id, None)
