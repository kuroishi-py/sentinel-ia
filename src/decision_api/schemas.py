"""Contrato de la API de decision.

Implementa el contrato definido en EP2_SENTINEL-IA_arc42_v2.md seccion 5.1
(POST /v1/score) y las clases principales de la seccion 5.3.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Decision(str, enum.Enum):
    """Decision que devuelve el servicio. RF3."""

    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class ScoreRequest(BaseModel):
    """Transaccion tokenizada que envia la pasarela de pagos. RF1.

    SENTINEL-IA nunca recibe el PAN, solo tokens (restriccion RT8).
    """

    id_tx: str = Field(min_length=8, max_length=64, description="Identificador unico de la transaccion")
    user_token: str = Field(min_length=8, max_length=128, description="Token del usuario, no el PAN")
    merchant_id: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0, le=100_000_000, description="Monto en la moneda indicada")
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217, por ejemplo CLP")
    country: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alfa-2")
    device_token: str | None = Field(default=None, max_length=128)
    mcc: str | None = Field(default=None, max_length=4, description="Merchant category code")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("currency", "country")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class ScoreResponse(BaseModel):
    """Respuesta al camino sincrono. RF2, RF3, RF9."""

    id_tx: str
    score: float = Field(ge=0.0, le=1.0)
    decision: Decision
    model_version: str
    reason_codes: list[str] = Field(default_factory=list)
    degraded: bool = Field(
        default=False,
        description="True si falto una feature o se agoto el presupuesto interno de latencia",
    )
    latency_ms: float = Field(ge=0.0)


class DecisionEvent(BaseModel):
    """Evento publicado en decision.v1. Es lo que queda en el log inmutable. RF5."""

    id_tx: str
    user_token: str
    merchant_id: str
    amount: float
    currency: str
    score: float
    decision: Decision
    model_version: str
    reason_codes: list[str]
    degraded: bool
    features: dict[str, float]
    decided_at: datetime


class Explanation(BaseModel):
    """Explicacion SHAP de un rechazo o revision. RF4."""

    id_tx: str
    base_value: float
    contributions: dict[str, float] = Field(
        description="Aporte de cada feature al score, ordenado por magnitud"
    )
    top_features: list[str]
    explained_at: datetime


class HealthStatus(BaseModel):
    status: str
    model_version: str | None = None
    model_loaded: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
