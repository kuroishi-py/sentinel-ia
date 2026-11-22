"""FeatureAssembler (EP2 seccion 5.2).

Lee el perfil del usuario y actualiza los contadores de velocidad en una sola
ida y vuelta al feature store. En la EP2 esto es un script Lua sobre Valkey; en
este esqueleto se usa una pipeline de Redis, que tiene el mismo efecto de una
sola llamada de red.

Si el feature store no responde, el servicio no se cae: devuelve valores por
defecto y marca la decision como degradada (EP2 seccion 8.5).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .schemas import ScoreRequest


@dataclass
class FeatureSet:
    """Features calculadas, mas la senal de si alguna falto."""

    values: dict[str, float]
    degraded: bool = False
    missing: tuple[str, ...] = ()

    def as_vector(self, names: list[str]) -> list[float]:
        """Vector en el orden que espera el modelo (feature parity, RT2)."""
        return [float(self.values.get(name, 0.0)) for name in names]


#: Perfil que se asume cuando el usuario no existe en el store todavia.
_DEFAULT_PROFILE = {
    "amount_avg": 0.0,
    "user_age_days": 0.0,
    "known_devices": "",
}


class FeatureAssembler:
    """Arma el vector de features para una transaccion."""

    def __init__(self, store=None, merchant_risk: dict[str, float] | None = None) -> None:
        self._store = store
        self._merchant_risk = merchant_risk or {}

    def build(self, request: ScoreRequest) -> FeatureSet:
        profile, counters, degraded, missing = self._read_state(request)

        amount_avg = float(profile.get("amount_avg") or 0.0)
        values: dict[str, float] = {
            "amount_log": math.log1p(request.amount),
            "amount_vs_user_avg": (request.amount / amount_avg) if amount_avg > 0 else 1.0,
            "tx_count_1h": float(counters.get("tx_count_1h", 0.0)),
            "tx_count_24h": float(counters.get("tx_count_24h", 0.0)),
            "amount_sum_24h": float(counters.get("amount_sum_24h", 0.0)),
            "distinct_merchants_24h": float(counters.get("distinct_merchants_24h", 0.0)),
            "user_age_days": float(profile.get("user_age_days") or 0.0),
            "merchant_risk": self._merchant_risk.get(request.merchant_id, 0.0),
            "is_foreign_country": 0.0 if request.country == "CL" else 1.0,
            "hour_of_day": float(request.timestamp.hour),
            "is_new_device": self._is_new_device(request, profile),
        }
        return FeatureSet(values=values, degraded=degraded, missing=missing)

    def _read_state(
        self, request: ScoreRequest
    ) -> tuple[dict[str, object], dict[str, float], bool, tuple[str, ...]]:
        """Lee perfil y contadores, y los incrementa en la misma operacion."""
        if self._store is None:
            return dict(_DEFAULT_PROFILE), {}, True, ("feature_store",)

        profile_key = f"profile:{request.user_token}"
        counter_key = f"vel:{request.user_token}"
        try:
            pipe = self._store.pipeline()
            pipe.hgetall(profile_key)
            pipe.hgetall(counter_key)
            # Los contadores se actualizan en la misma llamada: la transaccion
            # que se esta evaluando ya cuenta para la siguiente.
            pipe.hincrby(counter_key, "tx_count_1h", 1)
            pipe.hincrby(counter_key, "tx_count_24h", 1)
            pipe.hincrbyfloat(counter_key, "amount_sum_24h", request.amount)
            pipe.expire(counter_key, 86_400)
            raw_profile, raw_counters, *_ = pipe.execute()
        except Exception:  # noqa: BLE001 - cualquier falla del store degrada, no cae
            return dict(_DEFAULT_PROFILE), {}, True, ("feature_store",)

        profile = _decode(raw_profile) or dict(_DEFAULT_PROFILE)
        counters_raw = _decode(raw_counters)
        counters: dict[str, float] = {}
        for key, value in counters_raw.items():
            try:
                counters[key] = float(value)
            except (TypeError, ValueError):
                continue

        missing: tuple[str, ...] = ()
        if not raw_profile:
            # Usuario nuevo: no es una falla, es un caso valido sin historia.
            missing = ("profile",)
        return profile, counters, False, missing

    @staticmethod
    def _is_new_device(request: ScoreRequest, profile: dict[str, object]) -> float:
        if not request.device_token:
            return 1.0
        known = str(profile.get("known_devices") or "")
        return 0.0 if request.device_token in known.split(",") else 1.0


def _decode(raw) -> dict[str, str]:
    """Normaliza la respuesta de Redis, que puede venir en bytes."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        k = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        v = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        out[k] = v
    return out
