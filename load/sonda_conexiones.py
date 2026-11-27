"""Sonda de latencia con N conexiones persistentes, sin Locust ni gevent.

Para que sirve: separar la latencia del servicio de la del generador de carga.
Abre N conexiones TCP (por omision 116), cada una manda 1 solicitud por segundo
a /v1/score con el request completo en una sola escritura y TCP_NODELAY, y mide
desde el envio hasta el ultimo byte de la respuesta.

Con --partir-sin-nodelay manda cabeceras y cuerpo en dos escrituras sin
TCP_NODELAY: reproduce los ~40 ms que agrega la interaccion entre el algoritmo
de Nagle del cliente y el ACK retardado de Linux. Es una recomendacion para el
contrato con la pasarela: su cliente HTTP debe usar TCP_NODELAY (Go, Netty y
OkHttp lo hacen por omision).

Uso, dentro de la red de Docker (desde cualquier contenedor con Python):
    docker compose exec -T dashboard python - < load/sonda_conexiones.py
    docker compose exec -T dashboard python - --partir-sin-nodelay < load/sonda_conexiones.py
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import statistics
import threading
import time
import uuid

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="api")
parser.add_argument("--port", type=int, default=8000)
parser.add_argument("--conexiones", type=int, default=116)
parser.add_argument("--segundos", type=int, default=25)
parser.add_argument("--partir-sin-nodelay", action="store_true")
args = parser.parse_args()
KEY = os.environ.get("SENTINEL_API_KEY", "sentinel-demo-local")

latencies: list[float] = []
lock = threading.Lock()


def one_request(sock: socket.socket) -> float:
    n = random.randint(1, 20_000)
    body = json.dumps(
        {
            "id_tx": "tx-sonda" + uuid.uuid4().hex[:8] + "zz",
            "user_token": f"usr_{n:06d}",
            "merchant_id": "mrc_0002",
            "amount": 20_000,
            "currency": "CLP",
            "country": "CL",
            "device_token": f"dev_{n:06d}",
        }
    ).encode()
    head = (
        f"POST /v1/score HTTP/1.1\r\nHost: {args.host}\r\nX-API-Key: {KEY}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode()
    started = time.perf_counter()
    if args.partir_sin_nodelay:
        sock.sendall(head)
        sock.sendall(body)
    else:
        sock.sendall(head + body)
    data = b""
    while b"\r\n\r\n" not in data:
        data += sock.recv(4096)
    header, rest = data.split(b"\r\n\r\n", 1)
    length = int(next(line for line in header.split(b"\r\n") if line.lower().startswith(b"content-length")).split(b":")[1])
    while len(rest) < length:
        rest += sock.recv(4096)
    return (time.perf_counter() - started) * 1000


def connection() -> None:
    sock = socket.create_connection((args.host, args.port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 0 if args.partir_sin_nodelay else 1)
    time.sleep(random.random())
    end = time.time() + args.segundos
    while time.time() < end:
        elapsed = one_request(sock)
        with lock:
            latencies.append(elapsed)
        time.sleep(max(0.0, 1 - elapsed / 1000))


threads = [threading.Thread(target=connection) for _ in range(args.conexiones)]
for t in threads:
    t.start()
for t in threads:
    t.join()

latencies.sort()
mode = "cabeceras y cuerpo separados, sin TCP_NODELAY" if args.partir_sin_nodelay else "una escritura, TCP_NODELAY"
print(
    f"{args.conexiones} conexiones, 1 req/s c/u ({mode}): n={len(latencies)} "
    f"p50 {statistics.median(latencies):.1f} ms  p95 {latencies[int(0.95 * len(latencies))]:.1f} ms  "
    f"p99 {latencies[int(0.99 * len(latencies))]:.1f} ms"
)
