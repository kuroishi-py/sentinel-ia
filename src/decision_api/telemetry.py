"""Telemetry (EP2 seccion 5.2).

Expone en /metrics lo que la EP2 lista como metricas de servicio y de modelo
(seccion 8.3), en formato de texto de Prometheus. Ademas calcula p50, p95 y p99
en proceso, porque la pauta de la EP3 pide esos tres percentiles y tenerlos a
mano en el dashboard evita depender de que Prometheus este levantado.

Sin dependencias externas a proposito: una metrica que se cae porque falta un
paquete no sirve de evidencia.
"""

from __future__ import annotations

import threading
from collections import Counter, deque

from .schemas import Decision

#: Bordes del histograma de latencia, en milisegundos. Elegidos alrededor del
#: presupuesto del caso: 30 ms interno, 60 ms de corte, 80 ms de la pasarela.
LATENCY_BUCKETS_MS = (1, 2, 5, 10, 20, 30, 40, 60, 80, 100, 250, 500, 1000)

#: Cuantas latencias recientes se guardan para calcular percentiles.
SAMPLE_WINDOW = 10_000


class Telemetry:
    """Contadores y latencias del servicio."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._decisions: Counter[str] = Counter()
        self._counters: Counter[str] = Counter()
        self._buckets: Counter[int] = Counter()
        self._latencies: deque[float] = deque(maxlen=SAMPLE_WINDOW)
        self._latency_sum = 0.0
        self._requests = 0

    # --- registro ---------------------------------------------------------

    def observe(self, decision: Decision, latency_ms: float, degraded: bool) -> None:
        with self._lock:
            self._requests += 1
            self._decisions[decision.value] += 1
            self._latency_sum += latency_ms
            self._latencies.append(latency_ms)
            for edge in LATENCY_BUCKETS_MS:
                if latency_ms <= edge:
                    self._buckets[edge] += 1
            if degraded:
                self._counters["degraded"] += 1

    def rate_limited(self) -> None:
        self._bump("rate_limited")

    def guard_rejected(self) -> None:
        self._bump("guard_rejected")

    def idempotent_hit(self) -> None:
        self._bump("idempotent_hit")

    def budget_exceeded(self) -> None:
        self._bump("budget_exceeded")

    def _bump(self, name: str) -> None:
        with self._lock:
            self._counters[name] += 1

    # --- lectura ----------------------------------------------------------

    def quantiles(self) -> dict[str, float]:
        """p50, p95 y p99 de las latencias recientes, en milisegundos."""
        with self._lock:
            samples = sorted(self._latencies)
        if not samples:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        return {
            "p50": _percentile(samples, 0.50),
            "p95": _percentile(samples, 0.95),
            "p99": _percentile(samples, 0.99),
        }

    def snapshot(self) -> dict[str, object]:
        """Resumen para el dashboard."""
        with self._lock:
            total = self._requests
            decisions = dict(self._decisions)
            counters = dict(self._counters)
            mean = (self._latency_sum / total) if total else 0.0
        errors = counters.get("guard_rejected", 0) + counters.get("rate_limited", 0)
        return {
            "requests_total": total,
            "decisions": decisions,
            "counters": counters,
            "latency_mean_ms": round(mean, 3),
            "latency": self.quantiles(),
            "error_rate": round(errors / total, 6) if total else 0.0,
        }

    def render(self) -> str:
        """Formato de exposicion de Prometheus."""
        with self._lock:
            total = self._requests
            decisions = dict(self._decisions)
            counters = dict(self._counters)
            buckets = dict(self._buckets)
            latency_sum = self._latency_sum

        lines: list[str] = []

        lines.append("# HELP sentinel_requests_total Solicitudes de scoring atendidas.")
        lines.append("# TYPE sentinel_requests_total counter")
        lines.append(f"sentinel_requests_total {total}")

        lines.append("# HELP sentinel_decisions_total Decisiones por tipo.")
        lines.append("# TYPE sentinel_decisions_total counter")
        for value in (d.value for d in Decision):
            lines.append(f'sentinel_decisions_total{{decision="{value}"}} {decisions.get(value, 0)}')

        for name, help_text in (
            ("degraded", "Decisiones marcadas como degradadas."),
            ("budget_exceeded", "Solicitudes que excedieron el presupuesto interno de latencia."),
            ("guard_rejected", "Solicitudes rechazadas por un guardrail de entrada."),
            ("rate_limited", "Solicitudes rechazadas por cuota."),
            ("idempotent_hit", "Respuestas repetidas por idempotencia."),
        ):
            lines.append(f"# HELP sentinel_{name}_total {help_text}")
            lines.append(f"# TYPE sentinel_{name}_total counter")
            lines.append(f"sentinel_{name}_total {counters.get(name, 0)}")

        lines.append("# HELP sentinel_latency_ms Latencia de /v1/score en milisegundos.")
        lines.append("# TYPE sentinel_latency_ms histogram")
        for edge in LATENCY_BUCKETS_MS:
            lines.append(f'sentinel_latency_ms_bucket{{le="{edge}"}} {buckets.get(edge, 0)}')
        lines.append(f'sentinel_latency_ms_bucket{{le="+Inf"}} {total}')
        lines.append(f"sentinel_latency_ms_sum {latency_sum:.3f}")
        lines.append(f"sentinel_latency_ms_count {total}")

        return "\n".join(lines) + "\n"


def _percentile(sorted_samples: list[float], fraction: float) -> float:
    """Percentil por interpolacion lineal sobre una lista ya ordenada."""
    if not sorted_samples:
        return 0.0
    if len(sorted_samples) == 1:
        return round(sorted_samples[0], 3)
    position = fraction * (len(sorted_samples) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_samples) - 1)
    weight = position - lower
    value = sorted_samples[lower] * (1 - weight) + sorted_samples[upper] * weight
    return round(value, 3)
