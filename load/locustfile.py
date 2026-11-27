"""Prueba de carga con Locust.

Cubre el punto 2.3 de la pauta: pruebas de carga, latencia p50/p95/p99,
throughput y tasa de errores. Locust reporta los tres percentiles directo en su
interfaz y en el CSV que exporta.

Objetivos que vienen del caso (EP2 secciones 1.2 y 7.4):
    - pico proyectado: 116 tx/s
    - p99 en la pasarela: < 100 ms
    - p99 interno del servicio: < 30 ms
    - supuesto a validar: 120 solicitudes/s por tarea

Dos perfiles, que se eligen con la variable PERFIL:

    pico       (por omision) cada usuario manda exactamente 1 solicitud por
               segundo, asi que -u 116 son 116 tx/s: el pico del caso.
    capacidad  cada usuario espera entre 50 y 200 ms: sirve para saturar la
               tarea y encontrar donde se rompe. Con -u 116 son unas 700 req/s.

La primera version usaba siempre el segundo ritmo con -u 116 y lo llamaba «el
pico»: en realidad generaba seis veces el pico y chocaba con la cuota.

El trafico imita al de produccion: usuarios con historia, desde su celular de
siempre y por montos cercanos a su promedio, con un 2% de dispositivos nuevos y
un 2% de compras en el extranjero. Esa historia se precarga antes en Redis:

    docker compose exec api python -m src.demo.seed --users 20000

Uso (dentro de Docker, contra la tarea, como lo haria el balanceador):
    docker compose --profile carga run --rm locust

Uso (desde la maquina):
    SENTINEL_API_KEY=... locust -f load/locustfile.py --host http://localhost:8000 \\
           --headless -u 116 -r 20 -t 3m --csv reports/carga
"""

from __future__ import annotations

import base64
import os
import random
import sys
import uuid
from pathlib import Path

from locust import FastHttpUser, between, constant_throughput, events, task

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.demo.seed import user_average, user_token, usual_device  # noqa: E402

API_KEY = os.environ.get("SENTINEL_API_KEY", "sentinel-demo-local")
PERFIL = os.environ.get("PERFIL", "pico").lower()
#: Cuantos usuarios precargo src.demo.seed. Con 20.000 y 116 tx/s, en 3 minutos
#: cada usuario compra una vez en promedio, como en el pico real.
USERS = int(os.environ.get("USUARIOS_PRECARGADOS", "20000"))

MERCHANTS = [f"mrc_{i:04d}" for i in range(1, 301)]
FOREIGN = ["AR", "PE", "BR", "US", "ES"]

#: Nombre con que se reportan las solicitudes cortadas por cuota. Van aparte
#: para no esconderlas como exitos dentro de POST /v1/score.
RATE_LIMITED = "POST /v1/score [429 cuota]"


def random_amount(n: int) -> float:
    """Cerca del promedio del usuario, con una compra grande de vez en cuando."""
    amount = user_average(n) * random.lognormvariate(0, 0.5)
    if random.random() < 0.01:
        amount *= random.uniform(3, 8)
    return round(amount, 0)


def new_id() -> str:
    """id_tx sin rachas largas de digitos.

    InputGuard rechaza cualquier campo con 13 a 19 digitos seguidos que pase
    Luhn. Un UUID en hexadecimal cae en eso de vez en cuando (la primera
    corrida tuvo 68 respuestas 422 por eso); base32 practicamente nunca.
    """
    return "tx-" + base64.b32encode(uuid.uuid4().bytes).decode("ascii").rstrip("=")


class ScoringUser(FastHttpUser):
    """Simula a la pasarela de pagos llamando al scoring.

    FastHttpUser y no HttpUser: en Docker con 2 vCPU el cliente de ``requests``
    le suma ~20 ms a cada solicitud del lado de Locust, y eso se reportaba
    como si fuera latencia del servicio (medido el 26-11-2026 contra
    /health/live: 21 ms de mediana con HttpUser, 7 ms con FastHttpUser, 1,5 ms
    con una sonda independiente).
    """

    wait_time = constant_throughput(1) if PERFIL == "pico" else between(0.05, 0.2)
    default_headers = {"X-API-Key": API_KEY}

    def _post(self, payload: dict, name: str) -> None:
        with self.client.post(
            "/v1/score", json=payload, name=name, catch_response=True, timeout=2
        ) as response:
            if response.status_code == 429:
                # La cuota funcionando no es un error del servicio, pero tampoco
                # es una decision: se reporta en su propia fila.
                response.request_meta["name"] = RATE_LIMITED
                response.success()
                return
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            body = response.json()
            if body.get("degraded"):
                response.failure("decision degradada")
            elif body["latency_ms"] > 80:
                response.failure(f"latencia interna {body['latency_ms']:.1f} ms sobre el timeout")
            else:
                response.success()

    @task(95)
    def score_transaction(self) -> None:
        n = random.randint(1, USERS)
        new_device = random.random() < 0.02
        self._post(
            {
                "id_tx": new_id(),
                "user_token": user_token(n),
                "merchant_id": random.choice(MERCHANTS),
                "amount": random_amount(n),
                "currency": "CLP",
                "country": random.choice(FOREIGN) if random.random() < 0.02 else "CL",
                "device_token": f"dev_nuevo_{uuid.uuid4().hex[:8]}" if new_device else usual_device(n),
                "mcc": random.choice(["5812", "5411", "5999", "4111", "5311"]),
            },
            "POST /v1/score",
        )

    @task(4)
    def repeat_transaction(self) -> None:
        """Reenvia el mismo id_tx: ejercita el camino de idempotencia."""
        self._post(
            {
                "id_tx": "tx-idempotencia-fija-01",
                "user_token": user_token(1),
                "merchant_id": "mrc_0001",
                "amount": user_average(1),
                "currency": "CLP",
                "country": "CL",
                "device_token": usual_device(1),
            },
            "POST /v1/score (repetido)",
        )

    @task(1)
    def readiness(self) -> None:
        self.client.get("/health/ready", name="GET /health/ready")


@events.test_stop.add_listener
def report(environment, **kwargs) -> None:  # noqa: ARG001
    """Resumen contra los objetivos del caso, al terminar la prueba."""
    stats = environment.stats
    total = stats.total
    if not total.num_requests:
        return

    limited = stats.entries.get((RATE_LIMITED, "POST"))
    limited_count = limited.num_requests if limited else 0
    answered = total.num_requests - limited_count
    scoring = stats.entries.get(("POST /v1/score", "POST"))

    p50 = total.get_response_time_percentile(0.50)
    p95 = total.get_response_time_percentile(0.95)
    p99 = total.get_response_time_percentile(0.99)
    error_rate = total.num_failures / total.num_requests
    rps = total.total_rps

    print("\n" + "=" * 66)
    print(f"Resultado frente a los objetivos de SENTINEL-IA (perfil {PERFIL})")
    print("=" * 66)
    print(f"  Solicitudes      {total.num_requests}  ({limited_count} cortadas por cuota, {answered} atendidas)")
    print(f"  Throughput       {rps:.1f} req/s  (pico del caso: 116)")
    print(f"  p50              {p50:.1f} ms")
    print(f"  p95              {p95:.1f} ms")
    print(f"  p99              {p99:.1f} ms  (objetivo en la pasarela: < 100)")
    if scoring and scoring.num_requests:
        print(f"  p99 de /v1/score {scoring.get_response_time_percentile(0.99):.1f} ms")
    print(f"  Tasa de errores  {error_rate:.2%}  (objetivo: < 1%)")

    internal = _internal_stats(environment.host)
    if internal:
        lat = internal.get("latency", {})
        print(
            f"  Latencia interna (un worker, ultimas 10.000): p50 {lat.get('p50', 0):.2f} ms, "
            f"p95 {lat.get('p95', 0):.2f} ms, p99 {lat.get('p99', 0):.2f} ms  (objetivo p99: < 30)"
        )
    print("-" * 66)
    print(f"  p99 < 100 ms          {'CUMPLE' if p99 < 100 else 'NO CUMPLE'}")
    print(f"  errores < 1%          {'CUMPLE' if error_rate < 0.01 else 'NO CUMPLE'}")
    if PERFIL == "pico":
        # Locust arranca usuarios de a poco (-r): el promedio queda un par de
        # por ciento bajo el objetivo aunque el regimen estable lo alcance.
        print(f"  throughput >= 116/s   {'CUMPLE' if rps >= 116 * 0.97 else 'NO CUMPLE'} (tolerancia de 3% por la rampa)")
    if internal:
        ok = internal.get("latency", {}).get("p99", 1e9) < 30
        print(f"  p99 interno < 30 ms   {'CUMPLE' if ok else 'NO CUMPLE'}")
    print("=" * 66 + "\n")


def _internal_stats(host: str | None) -> dict:
    """Lee /v1/stats de la API. Solo responde si la carga va directo a la tarea."""
    if not host:
        return {}
    try:
        import requests

        response = requests.get(f"{host.rstrip('/')}/v1/stats", timeout=2)
        return response.json() if response.status_code == 200 else {}
    except Exception:  # noqa: BLE001
        return {}
