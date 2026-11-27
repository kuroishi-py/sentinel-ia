"""Casos CP-18 y CP-19: autenticacion y tolerancia a fallas del store.

Trazabilidad: pauta EP3 punto 2.2 (seguridad: autenticacion y rate limiting),
EP2 seccion 8.2 (confianza cero) y seccion 8.5 (manejo de errores). CP-19 nace
de la prueba de carga del 26-11-2026, donde un timeout de Redis en la
idempotencia devolvia 500 con la decision ya calculada.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from src.decision_api.guards import IdempotencyGuard  # noqa: E402
from src.decision_api.schemas import Decision, ScoreResponse  # noqa: E402

from .test_api import API_KEY, configure_env, payload  # noqa: E402


class BrokenStore:
    """Un Redis que siempre hace timeout."""

    def set(self, *args, **kwargs):
        raise TimeoutError("Timeout reading from socket")

    def get(self, *args, **kwargs):
        raise TimeoutError("Timeout reading from socket")


@pytest.fixture(scope="module")
def app_client():
    configure_env()
    from src.decision_api.config import reset_settings

    reset_settings()
    import importlib

    from src.decision_api import main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as test_client:
        yield test_client, main_module
    reset_settings()


class TestCP18Autenticacion:
    """CP-18: /v1/score exige API key; los endpoints de operacion no."""

    def test_sin_clave_devuelve_401(self, app_client) -> None:
        client, _ = app_client
        response = client.post("/v1/score", json=payload(id_tx="tx-cp18-sin"))
        assert response.status_code == 401
        assert response.headers.get("www-authenticate") == "ApiKey"

    def test_clave_invalida_devuelve_401(self, app_client) -> None:
        client, _ = app_client
        response = client.post(
            "/v1/score", json=payload(id_tx="tx-cp18-mala"), headers={"X-API-Key": "no-es-la-clave"}
        )
        assert response.status_code == 401

    def test_clave_valida_pasa(self, app_client) -> None:
        client, _ = app_client
        response = client.post("/v1/score", json=payload(id_tx="tx-cp18-ok"), headers={"X-API-Key": API_KEY})
        assert response.status_code == 200

    def test_los_health_checks_no_piden_clave(self, app_client) -> None:
        # El balanceador no tiene credenciales: si /health pidiera clave, la
        # tarea nunca quedaria sana.
        client, _ = app_client
        assert client.get("/health/ready").status_code == 200

    def test_el_contrato_declara_el_esquema_de_seguridad(self, app_client) -> None:
        client, _ = app_client
        spec = client.get("/openapi.json").json()
        schemes = spec["components"]["securitySchemes"]
        assert any(s.get("in") == "header" and s.get("name") == "X-API-Key" for s in schemes.values())

    def test_probar_claves_tambien_tiene_cuota(self) -> None:
        configure_env(RATE_LIMIT_RPS="1", RATE_LIMIT_BURST="2")
        from src.decision_api.config import reset_settings

        reset_settings()
        import importlib

        from src.decision_api import main as main_module

        importlib.reload(main_module)
        try:
            with TestClient(main_module.app) as limited:
                codes = [
                    limited.post(
                        "/v1/score", json=payload(id_tx=f"tx-cp18-fb-{i}"), headers={"X-API-Key": f"intento-{i}"}
                    ).status_code
                    for i in range(6)
                ]
            assert codes[0] == 401
            assert 429 in codes, f"fuerza bruta sin limite: {codes}"
        finally:
            configure_env()
            reset_settings()


class TestCP19FallasDelStore:
    """CP-19: si Redis falla, se pierde la idempotencia, no el pago."""

    def _response(self) -> ScoreResponse:
        return ScoreResponse(
            id_tx="tx-cp19-1",
            score=0.1,
            decision=Decision.APPROVE,
            model_version="v1",
            reason_codes=[],
            degraded=False,
            latency_ms=1.0,
        )

    def test_reserve_con_timeout_trata_la_transaccion_como_nueva(self) -> None:
        errors: list[str] = []
        guard = IdempotencyGuard(BrokenStore(), on_error=lambda op, exc: errors.append(op))
        assert guard.reserve("tx-cp19-1") is True
        assert errors == ["reserve"]

    def test_remember_y_recall_con_timeout_no_lanzan(self) -> None:
        errors: list[str] = []
        guard = IdempotencyGuard(BrokenStore(), on_error=lambda op, exc: errors.append(op))
        guard.remember(self._response())
        assert guard.recall("tx-cp19-1") is None
        assert errors == ["remember", "recall"]

    def test_la_api_responde_200_con_el_store_caido(self, app_client) -> None:
        client, main_module = app_client
        state = main_module.state
        original = state.idempotency
        state.idempotency = IdempotencyGuard(BrokenStore(), on_error=lambda op, exc: state.telemetry.store_error())
        try:
            response = client.post("/v1/score", json=payload(id_tx="tx-cp19-api"), headers={"X-API-Key": API_KEY})
        finally:
            state.idempotency = original
        assert response.status_code == 200
        assert "sentinel_store_error_total 2" in client.get("/metrics").text
