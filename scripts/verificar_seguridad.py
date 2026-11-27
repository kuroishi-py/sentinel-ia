"""Verificacion de seguridad del borde, para el informe (pauta 2.2).

Comprueba contra el sistema levantado con docker compose:

- que el borde negocia TLS 1.3 y rechaza TLS 1.2
- quien firma el certificado y para que nombre
- que /v1/score exige API key (401 sin ella o con una invalida)
- que los endpoints de operacion no se ven desde afuera (404)
- las cabeceras de seguridad (HSTS, nosniff)

Uso:
    docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt reports/caddy-root.crt
    python scripts/verificar_seguridad.py [https://localhost:8443] [reports/caddy-root.crt]
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
from urllib.parse import urlparse

import httpx

URL = sys.argv[1] if len(sys.argv) > 1 else "https://localhost:8443"
CA = sys.argv[2] if len(sys.argv) > 2 else "reports/caddy-root.crt"
KEY = os.environ.get("SENTINEL_API_KEY", "sentinel-demo-local")

host = urlparse(URL).hostname or "localhost"
port = urlparse(URL).port or 443
fails = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global fails
    fails += 0 if ok else 1
    print(f"[{'OK' if ok else 'FALLA'}] {label}{(' - ' + detail) if detail else ''}")


context = ssl.create_default_context(cafile=CA)
with socket.create_connection((host, port), timeout=5) as raw, context.wrap_socket(raw, server_hostname=host) as tls:
    cert = tls.getpeercert()
    issuer = dict(x[0] for x in cert["issuer"])
    san = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
    check("Version TLS negociada", tls.version() == "TLSv1.3", f"{tls.version()}, {tls.cipher()[0]}")
    check("Certificado valido para el host", host in san, f"SAN {san}, emisor {issuer.get('commonName')}")

legacy = ssl.create_default_context(cafile=CA)
legacy.maximum_version = ssl.TLSVersion.TLSv1_2
try:
    with socket.create_connection((host, port), timeout=5) as raw, legacy.wrap_socket(raw, server_hostname=host):
        check("TLS 1.2 rechazado", False, "el borde acepto TLS 1.2")
except ssl.SSLError as exc:
    check("TLS 1.2 rechazado", True, exc.reason or str(exc))

payload = {
    "id_tx": "tx-verificacion-seguridad",
    "user_token": "usr_verificacion",
    "merchant_id": "mrc_0002",
    "amount": 19990,
    "currency": "CLP",
    "country": "CL",
}
with httpx.Client(base_url=URL, verify=CA, timeout=10) as client:
    r = client.post("/v1/score", json=payload)
    check("Sin API key -> 401", r.status_code == 401, f"{r.status_code} {r.headers.get('www-authenticate')}")
    r = client.post("/v1/score", json=payload, headers={"X-API-Key": "clave-falsa"})
    check("API key invalida -> 401", r.status_code == 401, str(r.status_code))
    r = client.post("/v1/score", json=payload, headers={"X-API-Key": KEY})
    check("API key valida -> 200", r.status_code == 200, r.text[:120])
    check("HSTS", "max-age" in r.headers.get("strict-transport-security", ""), r.headers.get("strict-transport-security", ""))
    check("X-Content-Type-Options", r.headers.get("x-content-type-options") == "nosniff")
    check("Sin cabecera Server", "server" not in r.headers)
    for path in ("/metrics", "/v1/stats"):
        r = client.get(path)
        check(f"{path} no se ve desde afuera", r.status_code == 404, str(r.status_code))
    r = client.get("/health/ready")
    check("Health check publico para el balanceador", r.status_code == 200, r.json().get("status", ""))

print(f"\n{'Todo en orden' if not fails else f'{fails} verificaciones fallaron'}")
sys.exit(1 if fails else 0)
