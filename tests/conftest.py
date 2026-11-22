"""Fixtures compartidas."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# El paquete vive en src/, sin instalar.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.decision_api.schemas import ScoreRequest  # noqa: E402


@pytest.fixture
def tx() -> ScoreRequest:
    """Transaccion valida y corriente, para partir desde algo sano."""
    return ScoreRequest(
        id_tx="tx-00000001",
        user_token="usr_a1b2c3d4e5",
        merchant_id="mrc_0042",
        amount=19_990.0,
        currency="CLP",
        country="CL",
        device_token="dev_known_01",
        mcc="5812",
        timestamp=datetime(2026, 11, 22, 14, 30, tzinfo=UTC),
    )


def make_tx(**overrides) -> ScoreRequest:
    """Variante de la transaccion base."""
    base = {
        "id_tx": "tx-00000001",
        "user_token": "usr_a1b2c3d4e5",
        "merchant_id": "mrc_0042",
        "amount": 19_990.0,
        "currency": "CLP",
        "country": "CL",
        "device_token": "dev_known_01",
        "mcc": "5812",
        "timestamp": datetime(2026, 11, 22, 14, 30, tzinfo=UTC),
    }
    base.update(overrides)
    return ScoreRequest(**base)
