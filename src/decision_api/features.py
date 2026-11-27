"""FeatureAssembler (EP2 seccion 5.2).

Arma las 11 features del diseno para una transaccion. Se divide en tres piezas
para que la restriccion RT2 (feature parity) se cumpla por construccion y no
por disciplina:

- ``compute_features``  : funcion pura. Recibe la transaccion y el estado del
                          usuario y devuelve las features. La usan la API y el
                          simulador de entrenamiento (``src/training/simulate.py``),
                          asi que el modelo se entrena con exactamente el mismo
                          calculo con el que despues se le pregunta.
- ``FeatureState``      : de donde sale el estado del usuario. ``RedisFeatureState``
                          es el feature store (Valkey en la EP2) y lee y actualiza
                          todo en una sola ida y vuelta; ``MemoryFeatureState``
                          hace lo mismo en proceso, para correr sin Redis y para
                          reproducir la historia al entrenar.
- ``FeatureAssembler``  : junta las dos y decide cuando una decision queda
                          degradada.

Si el feature store no responde, el servicio no se cae: devuelve valores por
defecto y marca la decision como degradada (EP2 seccion 8.5).
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .schemas import ScoreRequest

logger = logging.getLogger(__name__)

#: Ventanas de los contadores de velocidad, en segundos.
WINDOW_1H = 3_600
WINDOW_24H = 86_400

#: Pais de operacion de la fintech. Una compra fuera de el es una senal.
HOME_COUNTRY = "CL"

#: Cuanto vive el perfil de un usuario sin actividad antes de expirar.
PROFILE_TTL_S = 400 * 86_400


@dataclass
class UserState:
    """Lo que el sistema sabe del usuario justo antes de esta transaccion.

    ``history`` son las transacciones anteriores de las ultimas 24 h como
    ``(timestamp, monto, comercio)``. La transaccion que se evalua no esta
    incluida: cuenta para la siguiente.
    """

    history: list[tuple[float, float, str]] = field(default_factory=list)
    first_seen: float | None = None
    amount_total: float = 0.0
    amount_count: int = 0
    #: Cuando se vio por primera vez el dispositivo de esta transaccion.
    #: None si no trae dispositivo o si nunca se habia visto.
    device_first_seen: float | None = None


class MerchantRisk:
    """Tasa historica de fraude por comercio, con un valor previo para los nuevos.

    La tabla sale del entrenamiento (``artifacts/merchant_risk.json``) y se
    calcula sobre un periodo anterior al de entrenamiento, para que la feature
    no filtre la etiqueta que el modelo intenta aprender.
    """

    def __init__(self, table: dict[str, float] | None = None, prior: float = 0.0) -> None:
        self._table = dict(table or {})
        self._prior = float(prior)

    @property
    def prior(self) -> float:
        return self._prior

    def get(self, merchant_id: str) -> float:
        return float(self._table.get(merchant_id, self._prior))

    def __len__(self) -> int:
        return len(self._table)

    @classmethod
    def from_file(cls, path: str | Path) -> MerchantRisk:
        path = Path(path)
        if not path.exists():
            logger.warning("Sin %s: merchant_risk usara 0 para todos los comercios", path)
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("No se pudo leer %s (%s): merchant_risk usara 0", path, exc)
            return cls()
        return cls(raw.get("merchants", {}), raw.get("prior", 0.0))


def compute_features(request: ScoreRequest, state: UserState, merchant_risk: MerchantRisk) -> dict[str, float]:
    """Las 11 features del diseno. Funcion pura: mismo input, mismo output."""
    now = request.timestamp.timestamp()
    history = [h for h in state.history if 0 <= now - h[0] <= WINDOW_24H]
    last_hour = [h for h in history if now - h[0] <= WINDOW_1H]

    amount_avg = state.amount_total / state.amount_count if state.amount_count else 0.0
    age_days = max(0.0, (now - state.first_seen) / 86_400) if state.first_seen is not None else 0.0

    return {
        "amount_log": math.log1p(request.amount),
        "amount_vs_user_avg": (request.amount / amount_avg) if amount_avg > 0 else 1.0,
        "tx_count_1h": float(len(last_hour)),
        "tx_count_24h": float(len(history)),
        "amount_sum_24h": float(sum(h[1] for h in history)),
        "distinct_merchants_24h": float(len({h[2] for h in history})),
        "user_age_days": round(age_days, 4),
        "merchant_risk": merchant_risk.get(request.merchant_id),
        "is_foreign_country": 0.0 if request.country == HOME_COUNTRY else 1.0,
        "hour_of_day": float(request.timestamp.hour),
        # Un dispositivo cuenta como nuevo durante sus primeras 24 h: una rafaga
        # de fraude desde un celular desconocido sigue marcada despues de la
        # primera compra, no solo en ella.
        "is_new_device": (
            0.0
            if state.device_first_seen is not None and now - state.device_first_seen > WINDOW_24H
            else 1.0
        ),
    }


# --- estado del usuario -----------------------------------------------------


class FeatureState(Protocol):
    """Lee el estado del usuario y lo actualiza con la transaccion actual."""

    def read_and_update(self, request: ScoreRequest) -> UserState: ...


class RedisFeatureState:
    """Feature store sobre Redis/Valkey.

    Tres claves por usuario:

    - ``vel:<user>``      ZSET con las transacciones de las ultimas 24 h
                          (miembro ``id_tx|monto|comercio``, score = timestamp)
    - ``profile:<user>``  HASH con ``first_seen``, ``amount_total`` y ``amount_count``
    - ``devices:<user>``  HASH dispositivo -> timestamp en que se vio por primera vez

    Todo, lectura y escritura, va en una sola pipeline: una ida y vuelta de red,
    que es lo que la EP2 presupuesta para el camino sincrono (seccion 6).
    """

    def __init__(self, client) -> None:
        self._client = client

    def read_and_update(self, request: ScoreRequest) -> UserState:
        now = request.timestamp.timestamp()
        user = request.user_token
        vel, profile, devices = f"vel:{user}", f"profile:{user}", f"devices:{user}"

        pipe = self._client.pipeline(transaction=False)
        # Lecturas: el estado previo a esta transaccion.
        pipe.zremrangebyscore(vel, "-inf", now - WINDOW_24H)
        pipe.zrangebyscore(vel, now - WINDOW_24H, now, withscores=True)
        pipe.hgetall(profile)
        if request.device_token:
            pipe.hget(devices, request.device_token)
        # Escrituras: esta transaccion ya cuenta para la siguiente.
        pipe.zadd(vel, {f"{request.id_tx}|{request.amount}|{request.merchant_id}": now})
        pipe.expire(vel, 2 * WINDOW_24H)
        pipe.hsetnx(profile, "first_seen", now)
        pipe.hincrbyfloat(profile, "amount_total", request.amount)
        pipe.hincrby(profile, "amount_count", 1)
        pipe.expire(profile, PROFILE_TTL_S)
        if request.device_token:
            pipe.hsetnx(devices, request.device_token, now)
            pipe.expire(devices, PROFILE_TTL_S)
        results = pipe.execute()

        raw_history, raw_profile = results[1], _decode(results[2])
        device_first_seen = _to_float(results[3]) if request.device_token else None

        history: list[tuple[float, float, str]] = []
        for member, score in raw_history:
            text = member.decode("utf-8") if isinstance(member, bytes) else str(member)
            parts = text.rsplit("|", 2)
            if len(parts) != 3:
                continue
            try:
                history.append((float(score), float(parts[1]), parts[2]))
            except ValueError:
                continue

        return UserState(
            history=history,
            first_seen=_to_float(raw_profile.get("first_seen")),
            amount_total=_to_float(raw_profile.get("amount_total")) or 0.0,
            amount_count=int(_to_float(raw_profile.get("amount_count")) or 0),
            device_first_seen=device_first_seen,
        )


class MemoryFeatureState:
    """El mismo feature store, en memoria del proceso.

    Sirve para dos cosas: correr la API sin Redis en desarrollo, y reproducir
    la historia de transacciones al entrenar, de modo que el entrenamiento vea
    lo mismo que vera la API.
    """

    def __init__(self) -> None:
        self._history: dict[str, deque[tuple[float, float, str]]] = defaultdict(deque)
        self._profiles: dict[str, list[float]] = {}
        self._devices: dict[str, dict[str, float]] = defaultdict(dict)
        self._lock = threading.Lock()

    def read_and_update(self, request: ScoreRequest) -> UserState:
        now = request.timestamp.timestamp()
        user = request.user_token
        with self._lock:
            window = self._history[user]
            while window and window[0][0] < now - WINDOW_24H:
                window.popleft()
            history = [h for h in window if h[0] <= now]
            profile = self._profiles.get(user)
            device_first_seen = self._devices[user].get(request.device_token) if request.device_token else None

            state = UserState(
                history=history,
                first_seen=profile[0] if profile else None,
                amount_total=profile[1] if profile else 0.0,
                amount_count=int(profile[2]) if profile else 0,
                device_first_seen=device_first_seen,
            )

            window.append((now, float(request.amount), request.merchant_id))
            if profile is None:
                self._profiles[user] = [now, float(request.amount), 1]
            else:
                profile[1] += float(request.amount)
                profile[2] += 1
            if request.device_token:
                self._devices[user].setdefault(request.device_token, now)
        return state


# --- ensamblador --------------------------------------------------------------


@dataclass
class FeatureSet:
    """Features calculadas, mas la senal de si alguna falto."""

    values: dict[str, float]
    degraded: bool = False
    missing: tuple[str, ...] = ()

    def as_vector(self, names: list[str]) -> list[float]:
        """Vector en el orden que espera el modelo (feature parity, RT2)."""
        return [float(self.values.get(name, 0.0)) for name in names]


class FeatureAssembler:
    """Arma el vector de features para una transaccion."""

    def __init__(self, store: FeatureState | None = None, merchant_risk: MerchantRisk | None = None) -> None:
        self._store = store
        self._merchant_risk = merchant_risk or MerchantRisk()

    def build(self, request: ScoreRequest) -> FeatureSet:
        if self._store is None:
            values = compute_features(request, UserState(), self._merchant_risk)
            return FeatureSet(values=values, degraded=True, missing=("feature_store",))
        try:
            state = self._store.read_and_update(request)
        except Exception as exc:  # noqa: BLE001 - cualquier falla del store degrada, no cae
            logger.warning("Feature store no disponible para %s: %s", request.id_tx, exc)
            values = compute_features(request, UserState(), self._merchant_risk)
            return FeatureSet(values=values, degraded=True, missing=("feature_store",))

        values = compute_features(request, state, self._merchant_risk)
        # Usuario nuevo: no es una falla, es un caso valido sin historia.
        missing = ("profile",) if state.amount_count == 0 else ()
        return FeatureSet(values=values, degraded=False, missing=missing)


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


def _to_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
