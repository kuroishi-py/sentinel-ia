"""Casos CP-09 y CP-10: rate limiting.

Trazabilidad: pauta EP3 punto 2.2 ("seguridad: autenticacion, encriptacion TLS,
rate limiting"). Es el control que faltaba en la EP2.

Se usa un reloj falso para que las pruebas no dependan del tiempo real.
"""

from __future__ import annotations

import pytest

from src.decision_api.ratelimit import TokenBucketLimiter


class FakeClock:
    """Reloj controlado a mano."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestCP09CuotaPorCliente:
    """CP-09: cada cliente tiene su propia cuota y la rafaga esta acotada."""

    def test_la_rafaga_se_agota_y_despues_bloquea(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(rate=10, burst=5, clock=clock)
        # Las primeras 5 entran sin que pase tiempo.
        assert all(limiter.allow("pasarela") for _ in range(5))
        # La sexta no.
        assert limiter.allow("pasarela") is False

    def test_los_clientes_no_comparten_cuota(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(rate=10, burst=2, clock=clock)
        assert limiter.allow("cliente-a") is True
        assert limiter.allow("cliente-a") is True
        assert limiter.allow("cliente-a") is False
        # El segundo cliente parte con su balde lleno.
        assert limiter.allow("cliente-b") is True

    def test_rate_y_burst_deben_ser_positivos(self) -> None:
        with pytest.raises(ValueError):
            TokenBucketLimiter(rate=0, burst=10)
        with pytest.raises(ValueError):
            TokenBucketLimiter(rate=10, burst=-1)


class TestCP10RellenoDelBalde:
    """CP-10: la cuota se repone con el tiempo, no de golpe."""

    def test_al_pasar_el_tiempo_vuelve_a_haber_cuota(self) -> None:
        # 0,25 s es exacto en binario, a diferencia de 0,1 o 0,2: asi la cuenta
        # de tokens no depende del redondeo del reloj.
        clock = FakeClock()
        limiter = TokenBucketLimiter(rate=8, burst=4, clock=clock)

        assert sum(limiter.allow("pasarela") for _ in range(4)) == 4
        assert limiter.allow("pasarela") is False

        # A 8 tokens/s, en 0,25 s se reponen exactamente 2.
        clock.advance(0.25)
        assert sum(limiter.allow("pasarela") for _ in range(4)) == 2

        # El relleno es continuo: otro cuarto de segundo, otros 2 tokens.
        clock.advance(0.25)
        assert sum(limiter.allow("pasarela") for _ in range(4)) == 2

    def test_el_balde_no_pasa_de_burst(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(rate=10, burst=3, clock=clock)
        limiter.allow("pasarela")
        # Mucho tiempo detenido no acumula credito infinito.
        clock.advance(3600)
        assert sum(limiter.allow("pasarela") for _ in range(10)) == 3

    def test_retry_after_estima_la_espera(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(rate=10, burst=1, clock=clock)
        assert limiter.allow("pasarela") is True
        assert limiter.allow("pasarela") is False
        # Falta 1 token a 10 por segundo: 0,1 s.
        assert limiter.retry_after("pasarela") == pytest.approx(0.1, abs=1e-6)

    def test_retry_after_es_cero_si_hay_cuota(self) -> None:
        limiter = TokenBucketLimiter(rate=10, burst=5, clock=FakeClock())
        assert limiter.retry_after("nuevo") == 0.0
