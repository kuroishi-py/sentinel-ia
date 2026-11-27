"""Precarga del feature store para la prueba de carga.

Por que hace falta: en produccion casi todas las transacciones son de usuarios
con historia y desde su celular de siempre. Si la prueba de carga manda usuarios
y dispositivos que el feature store nunca vio, el modelo ve «cuenta nueva +
dispositivo nuevo» en cada solicitud y manda la mitad a revision. Eso no mide
el pico del caso, mide un ataque masivo. La primera corrida en Docker
(26-11-2026) tuvo 51% de revisiones y rechazos por eso.

Este script deja en Redis el perfil y el dispositivo habitual de N usuarios,
con la misma formula que usa ``load/locustfile.py`` para generar sus compras.

Uso:
    docker compose exec api python -m src.demo.seed --users 20000
"""

from __future__ import annotations

import argparse
import time

DAY = 86_400


def user_token(n: int) -> str:
    return f"usr_{n:06d}"


def usual_device(n: int) -> str:
    return f"dev_{n:06d}"


def user_average(n: int) -> float:
    """Gasto promedio del usuario, entre 10.000 y 60.000 CLP, determinista."""
    return 10_000.0 + (n * 7_919) % 50_000


def seed(redis_url: str, users: int, now: float | None = None) -> int:
    import redis

    client = redis.Redis.from_url(redis_url, socket_timeout=5)
    now = now or time.time()
    pipe = client.pipeline(transaction=False)
    for n in range(1, users + 1):
        user = user_token(n)
        count = 20 + (n * 31) % 280
        age_days = 60 + (n * 13) % 840
        pipe.hset(
            f"profile:{user}",
            mapping={
                "first_seen": now - age_days * DAY,
                "amount_total": user_average(n) * count,
                "amount_count": count,
            },
        )
        pipe.hset(f"devices:{user}", usual_device(n), now - min(age_days, 30 + n % 300) * DAY)
        pipe.delete(f"vel:{user}")
        if n % 2_000 == 0:
            pipe.execute()
    pipe.execute()
    return users


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="Precarga perfiles en el feature store")
    parser.add_argument("--users", type=int, default=20_000)
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    args = parser.parse_args()
    started = time.perf_counter()
    count = seed(args.redis_url, args.users)
    print(f"{count} usuarios precargados en {args.redis_url} en {time.perf_counter() - started:.1f} s")


if __name__ == "__main__":
    main()
