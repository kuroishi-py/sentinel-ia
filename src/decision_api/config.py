"""Configuracion por variables de entorno.

Ningun secreto vive en el codigo ni en la imagen (EP2 seccion 8.2): se inyectan
en la definicion de tarea. Aqui solo se leen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Parametros de operacion de decision-api."""

    # --- Modelo -----------------------------------------------------------
    # "local"     : artefacto .json en disco (desarrollo y pruebas)
    # "sagemaker" : endpoint de SageMaker (lo que exige la pauta de la EP3)
    scorer_backend: str = field(default_factory=lambda: _env("SCORER_BACKEND", "local"))
    model_path: str = field(default_factory=lambda: _env("MODEL_PATH", "artifacts/model.json"))
    model_version: str = field(default_factory=lambda: _env("MODEL_VERSION", "dev-0"))
    sagemaker_endpoint: str = field(default_factory=lambda: _env("SAGEMAKER_ENDPOINT", ""))
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "sa-east-1"))

    # --- Presupuesto de latencia (EP2 RNF1) -------------------------------
    # La pasarela corta a 80 ms; ScoreRouter corta a 60 ms internos y devuelve
    # una decision degradada antes de que el timeout lo haga por nosotros.
    internal_budget_ms: float = field(default_factory=lambda: _env_float("INTERNAL_BUDGET_MS", 60.0))

    # --- Feature store ----------------------------------------------------
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379/0"))
    idempotency_ttl_s: int = field(default_factory=lambda: _env_int("IDEMPOTENCY_TTL_S", 86_400))

    # --- Bus de eventos ---------------------------------------------------
    # La EP2 eligio Kinesis (D15). La pauta de la EP3 pide RabbitMQ o Kafka,
    # asi que el tramo asincrono que se demuestra usa RabbitMQ.
    amqp_url: str = field(default_factory=lambda: _env("AMQP_URL", "amqp://guest:guest@localhost:5672/"))
    decision_exchange: str = field(default_factory=lambda: _env("DECISION_EXCHANGE", "decision.v1"))
    publisher_enabled: bool = field(default_factory=lambda: _env_bool("PUBLISHER_ENABLED", True))

    # --- Politica (EP2 seccion 5.1, PolicyEngine) -------------------------
    threshold_review: float = field(default_factory=lambda: _env_float("THRESHOLD_REVIEW", 0.45))
    threshold_reject: float = field(default_factory=lambda: _env_float("THRESHOLD_REJECT", 0.80))
    amount_review_floor: float = field(default_factory=lambda: _env_float("AMOUNT_REVIEW_FLOOR", 2_000_000.0))

    # --- Rate limiting ----------------------------------------------------
    # Lo pide la pauta de la EP3 en el punto 2.2 y no estaba en la EP2.
    rate_limit_enabled: bool = field(default_factory=lambda: _env_bool("RATE_LIMIT_ENABLED", True))
    rate_limit_rps: int = field(default_factory=lambda: _env_int("RATE_LIMIT_RPS", 200))
    rate_limit_burst: int = field(default_factory=lambda: _env_int("RATE_LIMIT_BURST", 400))

    @property
    def feature_names(self) -> list[str]:
        """Features que consume el modelo, en orden fijo.

        El mismo orden se usa en entrenamiento e inferencia (restriccion RT2:
        feature parity).
        """
        return [
            "amount_log",
            "amount_vs_user_avg",
            "tx_count_1h",
            "tx_count_24h",
            "amount_sum_24h",
            "distinct_merchants_24h",
            "user_age_days",
            "merchant_risk",
            "is_foreign_country",
            "hour_of_day",
            "is_new_device",
        ]


_settings: Settings | None = None


def get_settings() -> Settings:
    """Settings cacheadas por proceso."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Solo para pruebas: fuerza la relectura del entorno."""
    global _settings
    _settings = None
