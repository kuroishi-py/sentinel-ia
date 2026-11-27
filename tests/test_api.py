"""Casos CP-13 a CP-16: pruebas de integracion sobre la API.

A diferencia de las anteriores, estas levantan la aplicacion completa: FastAPI,
guardrails, features, scorer de respaldo, politica y telemetria trabajando
juntos. Cubren el "pruebas unitarias e integracion" de la pauta punto 2.3.

No necesitan Redis ni RabbitMQ: la API usa el feature store en memoria, que
es precisamente uno de los comportamientos que se quiere verificar.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

API_KEY = "clave-de-prueba"
HEADERS = {"X-API-Key": API_KEY}


def configure_env(**overrides: str) -> None:
    """Entorno comun de las pruebas de integracion."""
    env = {
        "PUBLISHER_ENABLED": "false",
        "SCORER_BACKEND": "local",
        "MODEL_PATH": "artifacts/no-existe.json",
        "FEATURE_STORE": "memory",
        "AUTH_ENABLED": "true",
        "API_KEYS": f"pasarela:{API_KEY}",
        "RATE_LIMIT_RPS": "1000",
        "RATE_LIMIT_BURST": "2000",
    }
    env.update(overrides)
    os.environ.update(env)


@pytest.fixture(scope="module")
def client():
    # Sin bus de eventos: las pruebas no dependen de RabbitMQ.
    configure_env()

    from src.decision_api.config import reset_settings

    reset_settings()
    from src.decision_api.main import app

    with TestClient(app, headers=HEADERS) as test_client:
        yield test_client


def payload(**overrides) -> dict:
    base = {
        "id_tx": "tx-integracion-1",
        "user_token": "usr_integracion",
        "merchant_id": "mrc_0042",
        "amount": 19990.0,
        "currency": "CLP",
        "country": "CL",
        "device_token": "dev_01",
        "mcc": "5812",
    }
    base.update(overrides)
    return base


class TestCP13ContratoDeLaAPI:
    """CP-13: /v1/score cumple el contrato de la EP2 seccion 5.1."""

    def test_respuesta_trae_todos_los_campos(self, client) -> None:
        response = client.post("/v1/score", json=payload(id_tx="tx-cp13-1"))
        assert response.status_code == 200

        body = response.json()
        for field in ("id_tx", "score", "decision", "model_version", "reason_codes", "degraded", "latency_ms"):
            assert field in body, f"falta {field} en la respuesta"
        assert 0.0 <= body["score"] <= 1.0
        assert body["decision"] in {"APPROVE", "REVIEW", "REJECT"}
        assert body["id_tx"] == "tx-cp13-1"

    def test_el_esquema_openapi_se_publica(self, client) -> None:
        # Es el requisito de documentacion Swagger/OpenAPI de la pauta 1.3.
        response = client.get("/openapi.json")
        assert response.status_code == 200
        spec = response.json()
        assert "/v1/score" in spec["paths"]
        assert "post" in spec["paths"]["/v1/score"]

    def test_esquema_invalido_devuelve_422(self, client) -> None:
        response = client.post("/v1/score", json={"id_tx": "x"})
        assert response.status_code == 422

    def test_monto_negativo_se_rechaza(self, client) -> None:
        response = client.post("/v1/score", json=payload(id_tx="tx-cp13-2", amount=-5000))
        assert response.status_code == 422


class TestCP14GuardrailEnCaliente:
    """CP-14: un PAN en la solicitud se rechaza con 422, no con 500."""

    def test_pan_en_el_token_devuelve_422(self, client) -> None:
        response = client.post(
            "/v1/score",
            json=payload(id_tx="tx-cp14-1", user_token="4111111111111111"),
        )
        assert response.status_code == 422
        assert response.json()["field"] == "user_token"


class TestCP15IdempotenciaExtremoAExtremo:
    """CP-15: repetir un id_tx devuelve la misma decision."""

    def test_la_segunda_llamada_repite_la_decision(self, client) -> None:
        body = payload(id_tx="tx-cp15-unico")
        first = client.post("/v1/score", json=body)
        second = client.post("/v1/score", json=body)

        assert first.status_code == second.status_code == 200
        assert second.headers.get("X-Idempotent-Replay") == "true"
        assert first.json()["decision"] == second.json()["decision"]
        assert first.json()["score"] == second.json()["score"]

    def test_cambiar_el_monto_no_altera_la_decision_guardada(self, client) -> None:
        # Idempotencia por id_tx: el primer resultado es el que vale.
        body = payload(id_tx="tx-cp15-estable", amount=10_000.0)
        first = client.post("/v1/score", json=body).json()
        second = client.post("/v1/score", json={**body, "amount": 9_000_000.0}).json()
        assert second["decision"] == first["decision"]


class TestCP16SaludYMetricas:
    """CP-16: health checks y /metrics, que son la evidencia de monitoreo."""

    def test_health_live_responde(self, client) -> None:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    def test_health_ready_indica_el_backend(self, client) -> None:
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["details"]["backend"] == "local"

    def test_metrics_expone_el_formato_prometheus(self, client) -> None:
        client.post("/v1/score", json=payload(id_tx="tx-cp16-1"))
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "sentinel_requests_total" in response.text
        assert "sentinel_latency_ms_bucket" in response.text

    def test_sin_modelo_nada_se_aprueba_a_ciegas(self, client) -> None:
        # MODEL_PATH apunta a un archivo inexistente, asi que corre el scorer
        # de respaldo: todo debe ir a revision humana.
        response = client.post("/v1/score", json=payload(id_tx="tx-cp16-2"))
        assert response.json()["decision"] in {"REVIEW", "REJECT"}


class TestCP17RateLimitEnCaliente:
    """CP-17: la API devuelve 429 cuando se pasa la cuota."""

    def test_cuota_baja_bloquea_y_trae_retry_after(self) -> None:
        configure_env(RATE_LIMIT_RPS="1", RATE_LIMIT_BURST="2")

        from src.decision_api.config import reset_settings

        reset_settings()
        import importlib

        from src.decision_api import main as main_module

        importlib.reload(main_module)

        try:
            with TestClient(main_module.app, headers=HEADERS) as limited:
                codes = [
                    limited.post("/v1/score", json=payload(id_tx=f"tx-cp17-{i}")).status_code
                    for i in range(6)
                ]
            assert 429 in codes, f"nunca se aplico la cuota: {codes}"
            assert codes[0] == 200
        finally:
            os.environ["RATE_LIMIT_RPS"] = "1000"
            os.environ["RATE_LIMIT_BURST"] = "2000"
            reset_settings()
