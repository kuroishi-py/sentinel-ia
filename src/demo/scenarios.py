"""Escenarios de demostracion, reproducibles.

Una sola transaccion suelta no dice mucho: las features de velocidad, el
promedio del usuario y la antiguedad del dispositivo salen de la historia. Estos
escenarios arman esa historia usando el campo ``timestamp`` del contrato (la
pasarela informa cuando ocurrio cada compra) y despues disparan el patron.

Los usan el dashboard (botones de la pestana «Probar»), la prueba de
interoperabilidad de punta a punta y ``scripts/evidencias.py``.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta


def _id(prefix: str = "tx") -> str:
    """Identificador sin rachas de digitos que InputGuard confunda con un PAN."""
    return f"{prefix}-" + base64.b32encode(uuid.uuid4().bytes).decode("ascii").rstrip("=")


def new_user() -> str:
    return _id("usr_demo")


def _tx(user: str, when: datetime, amount: float, merchant: str, country: str, device: str) -> dict:
    return {
        "id_tx": _id(),
        "user_token": user,
        "merchant_id": merchant,
        "amount": round(amount, 0),
        "currency": "CLP",
        "country": country,
        "device_token": device,
        "mcc": "5411",
        "timestamp": when.astimezone(UTC).isoformat(),
    }


def normal_history(user: str, now: datetime | None = None, days: int = 30) -> list[dict]:
    """Un mes de compras corrientes desde el celular de siempre, en Chile."""
    now = now or datetime.now(UTC)
    device = f"dev_{user}_habitual"
    out = []
    for i in range(12):
        when = now - timedelta(days=days - i * (days / 12), hours=-(i % 5))
        out.append(_tx(user, when, 15_000 + 1_500 * (i % 4), f"mrc_{(i % 4) + 1:04d}", "CL", device))
    return out


def normal_purchase(user: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    return _tx(user, now, 18_990, "mrc_0002", "CL", f"dev_{user}_habitual")


def account_takeover(user: str, risky_merchant: str, now: datetime | None = None) -> list[dict]:
    """Rafaga desde un dispositivo nuevo en el extranjero, en un comercio de riesgo."""
    now = now or datetime.now(UTC)
    device = f"dev_{user}_desconocido"
    amounts = [180_000, 240_000, 310_000, 390_000]
    return [
        _tx(user, now - timedelta(minutes=6 * (len(amounts) - 1 - k)), amount, risky_merchant, "AR", device)
        for k, amount in enumerate(amounts)
    ]


def card_testing(user: str, now: datetime | None = None) -> list[dict]:
    """Muchas compras chicas en minutos y en comercios distintos, y una grande al final."""
    now = now or datetime.now(UTC)
    device = f"dev_{user}_bot"
    probes = [_tx(user, now - timedelta(minutes=2 * (9 - k)), 990 + 150 * k, f"mrc_{100 + k:04d}", "CL", device) for k in range(9)]
    probes.append(_tx(user, now, 250_000, f"mrc_{120:04d}", "CL", device))
    return probes
