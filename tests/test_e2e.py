"""Caso CP-23: interoperabilidad de punta a punta, con el sistema levantado.

Cubre las «pruebas de interoperabilidad» de la pauta (punto 2.2): una
transaccion entra por el borde TLS, se autentica, se decide con features de
Redis, se publica en RabbitMQ, la consume shap-worker y termina como un caso
explicado que el dashboard puede mostrar. Si cualquiera de esos tramos no
conversa con el siguiente, esta prueba falla.

No corre por omision: necesita ``docker compose up``. Para correrla:

    docker compose up -d --build
    docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt reports/caddy-root.crt
    SENTINEL_E2E_URL=https://localhost:8443 pytest -m e2e -v
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

httpx = pytest.importorskip("httpx")

from src.demo import scenarios  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("SENTINEL_E2E_URL", "")
CA = os.environ.get("SENTINEL_E2E_CA", str(ROOT / "reports" / "caddy-root.crt"))
API_KEY = os.environ.get("SENTINEL_API_KEY", "sentinel-demo-local")
CASES = Path(os.environ.get("SENTINEL_CASES", str(ROOT / "data" / "cases.jsonl")))
RABBIT_API = os.environ.get("RABBITMQ_API", "http://localhost:15672/api")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BASE_URL, reason="define SENTINEL_E2E_URL con el sistema levantado"),
]


@pytest.fixture(scope="module")
def client():
    verify = CA if BASE_URL.startswith("https") else False
    with httpx.Client(base_url=BASE_URL, verify=verify, headers={"X-API-Key": API_KEY}, timeout=10) as c:
        yield c


def read_case(id_tx: str, seconds: float = 15.0) -> dict | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if CASES.exists():
            for line in reversed(CASES.read_text(encoding="utf-8").splitlines()):
                if line.strip() and json.loads(line).get("id_tx") == id_tx:
                    return json.loads(line)
        time.sleep(0.5)
    return None


class TestCP23Interoperabilidad:
    """CP-23: borde TLS -> API -> Redis -> RabbitMQ -> shap-worker -> caso."""

    def test_el_borde_solo_habla_tls_13(self) -> None:
        if not BASE_URL.startswith("https"):
            pytest.skip("la URL no es https")
        url = urlparse(BASE_URL)
        context = ssl.create_default_context(cafile=CA)
        with (
            socket.create_connection((url.hostname, url.port or 443), timeout=5) as raw,
            context.wrap_socket(raw, server_hostname=url.hostname) as tls,
        ):
            assert tls.version() == "TLSv1.3"

        legacy = ssl.create_default_context(cafile=CA)
        legacy.maximum_version = ssl.TLSVersion.TLSv1_2
        with pytest.raises(ssl.SSLError), socket.create_connection((url.hostname, url.port or 443), timeout=5) as raw:
            legacy.wrap_socket(raw, server_hostname=url.hostname)

    def test_los_endpoints_de_operacion_no_se_ven_desde_afuera(self, client) -> None:
        assert client.get("/metrics").status_code == 404
        assert client.get("/v1/stats").status_code == 404
        assert client.get("/health/ready").json()["details"]["feature_store"] == "redis"

    def test_sin_clave_el_borde_no_deja_pasar(self, client) -> None:
        payload = scenarios.normal_purchase(scenarios.new_user())
        assert client.post("/v1/score", json=payload, headers={"X-API-Key": ""}).status_code == 401

    def test_una_toma_de_cuenta_termina_en_un_caso_explicado(self, client) -> None:
        user = scenarios.new_user()
        for payload in scenarios.normal_history(user):
            assert client.post("/v1/score", json=payload).status_code == 200
        burst = scenarios.account_takeover(user, "mrc_0280")
        results = [client.post("/v1/score", json=p).json() for p in burst]
        assert results[-1]["decision"] == "REJECT", results

        case = read_case(burst[-1]["id_tx"])
        assert case is not None, "el caso no llego: revisa RabbitMQ y shap-worker"
        assert case["decision"] == "REJECT"
        assert case["explanation"] is not None
        # La explicacion habla en el idioma de las features del diseno, y los
        # valores con que se decidio viajaron por el bus (features de Redis).
        assert set(case["explanation"]["top_features"]) <= set(case["features"])
        assert case["features"]["tx_count_1h"] >= 2
        assert case["features"]["is_new_device"] == 1.0
        assert case["features"]["is_foreign_country"] == 1.0

    def test_una_compra_normal_no_genera_caso(self, client) -> None:
        user = scenarios.new_user()
        for payload in scenarios.normal_history(user):
            client.post("/v1/score", json=payload)
        normal = scenarios.normal_purchase(user)
        assert client.post("/v1/score", json=normal).json()["decision"] == "APPROVE"
        assert read_case(normal["id_tx"], seconds=3) is None

    def test_rabbitmq_tiene_la_cola_enlazada_y_un_consumidor(self) -> None:
        try:
            queue = httpx.get(f"{RABBIT_API}/queues/%2F/shap.cases", auth=("guest", "guest"), timeout=5)
            bindings = httpx.get(
                f"{RABBIT_API}/exchanges/%2F/decision.v1/bindings/source", auth=("guest", "guest"), timeout=5
            )
        except httpx.HTTPError:
            pytest.skip("sin acceso a la API de administracion de RabbitMQ")
        assert queue.status_code == 200
        assert queue.json()["consumers"] >= 1
        assert any(b["destination"] == "shap.cases" for b in bindings.json())
