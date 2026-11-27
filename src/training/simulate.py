"""Simulador de transacciones con las features del diseno (issue #1, opcion C).

Por que existe: el dataset publico de la ULB tiene metricas reales, pero sus
features son componentes PCA (``V1``-``V28``). Un modelo entrenado con ellas no
se puede servir por ``/v1/score``, porque la pasarela no manda componentes
principales, y su explicacion SHAP («V14 aporto 0,31») no le sirve a un analista
de Fraud Ops. La pauta pide interpretabilidad *para la toma de decisiones*.

Este modulo genera una historia de transacciones por usuario con los patrones
que el caso describe y calcula las features **reproduciendo esa historia por el
mismo codigo que usa la API** (``compute_features`` sobre ``MemoryFeatureState``).
Asi la restriccion RT2 (feature parity) se cumple por construccion: el modelo se
entrena con exactamente el mismo calculo con el que despues se le pregunta.

Lo que el simulador NO es: evidencia de desempeno en datos reales. Esa la da el
benchmark de la ULB (``artifacts/benchmark_ulb/``). Las metricas de este modelo
miden si aprende los patrones de fraude descritos, no cuanto fraude real detecta.

Patrones de fraude (cerca de 0,5% de las transacciones, como el caso):

- **Toma de cuenta (ATO):** rafaga de compras desde un dispositivo nuevo, a
  menudo desde el extranjero, de noche y en comercios de riesgo, por montos
  varias veces sobre el promedio del usuario. Un 25% es «lento y bajo» y un 20%
  usa el dispositivo del propio usuario (celular robado): son los dificiles.
- **Prueba de tarjeta:** muchas compras chicas en pocos minutos y en comercios
  distintos, y al final una o dos grandes.
- **Cuenta nueva fraudulenta:** usuario recien creado que compra montos altos en
  comercios de riesgo durante sus primeros dias.

Y comportamientos legitimos que se parecen al fraude, para que el problema no
sea trivial: viajes al extranjero, cambio de celular, compras grandes ocasionales
y tardes de compras con varias transacciones seguidas.
"""

from __future__ import annotations

import base64
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.decision_api.config import Settings
from src.decision_api.features import MemoryFeatureState, MerchantRisk, compute_features
from src.decision_api.schemas import ScoreRequest

logger = logging.getLogger("simulate")

#: Inicio de la historia simulada (UTC).
START = datetime(2026, 7, 1, tzinfo=UTC)
#: Chile continental, aproximado. Las horas de los patrones se piensan en hora
#: local y se guardan en UTC, que es como llegan a la API.
LOCAL_OFFSET_H = -3

#: (mcc, peso, es_de_riesgo)
CATEGORIES = [
    ("5411", 0.22, False),  # supermercados
    ("5812", 0.18, False),  # restaurantes
    ("5541", 0.10, False),  # combustible
    ("4111", 0.10, False),  # transporte
    ("5311", 0.12, False),  # grandes tiendas
    ("5999", 0.10, False),  # comercio minorista variado
    ("5732", 0.08, True),  # electronica
    ("4814", 0.04, True),  # recargas telefonicas
    ("5944", 0.03, True),  # joyeria
    ("7995", 0.03, True),  # apuestas
]
FOREIGN = ["AR", "PE", "BR", "US", "ES", "MX"]

#: Distribucion de la hora local de una compra legitima (0 a 23 h).
LEGIT_HOURS = [
    0.6, 0.3, 0.2, 0.1, 0.1, 0.2, 0.6, 1.5, 3.0, 4.0, 4.5, 5.0,
    6.0, 6.0, 5.0, 4.5, 4.5, 5.0, 6.0, 6.5, 6.0, 4.5, 3.0, 1.5,
]


@dataclass(frozen=True)
class SimulationConfig:
    n_users: int = 4_000
    n_merchants: int = 300
    days: int = 70
    #: Dias iniciales: calientan el feature store y dan la tabla de riesgo por
    #: comercio. No entran al entrenamiento.
    reference_days: int = 21
    #: Dias finales: conjunto de prueba. La particion es temporal, no aleatoria,
    #: porque en produccion el modelo siempre predice el futuro.
    test_days: int = 14
    ato_events: int = 240
    card_testing_events: int = 60
    fraud_accounts: int = 70
    seed: int = 42


def _local(day: int, hour: float) -> datetime:
    """Momento en UTC para un dia de la simulacion y una hora local."""
    return START + timedelta(days=day, hours=hour - LOCAL_OFFSET_H)


def _short_id(rng) -> str:
    """Identificador sin rachas largas de digitos.

    InputGuard rechaza cualquier campo con 13 a 19 digitos seguidos que pase
    Luhn. Un identificador hexadecimal puede caer en eso por azar; base32 casi
    nunca.
    """
    return base64.b32encode(rng.bytes(10)).decode("ascii").rstrip("=")


def simulate(config: SimulationConfig | None = None):
    """Genera la historia y devuelve ``(frame, merchant_risk)``.

    ``frame`` trae una fila por transaccion con sus 11 features, la etiqueta
    ``Class``, el escenario que la genero y el periodo (reference, train, test).
    """
    import numpy as np
    import pandas as pd

    cfg = config or SimulationConfig()
    rng = np.random.default_rng(cfg.seed)

    # --- comercios -----------------------------------------------------------
    cat_weights = np.array([c[1] for c in CATEGORIES])
    cat_index = rng.choice(len(CATEGORIES), size=cfg.n_merchants, p=cat_weights / cat_weights.sum())
    merchants = [f"mrc_{i:04d}" for i in range(1, cfg.n_merchants + 1)]
    merchant_mcc = {m: CATEGORIES[c][0] for m, c in zip(merchants, cat_index, strict=True)}
    risky = [m for m, c in zip(merchants, cat_index, strict=True) if CATEGORIES[c][2]]
    # Los defraudadores se concentran en unos pocos comercios de riesgo.
    hot = list(rng.choice(risky, size=max(3, len(risky) // 3), replace=False))
    popularity = 1.0 / np.arange(1, cfg.n_merchants + 1) ** 0.8
    rng.shuffle(popularity)
    popularity /= popularity.sum()

    rows: list[dict] = []

    def add(ts: datetime, user: str, merchant: str, amount: float, country: str, device: str, scenario: str, fraud: int):
        rows.append(
            {
                "ts": ts,
                "id_tx": f"tx-{_short_id(rng)}",
                "user_token": user,
                "merchant_id": merchant,
                "mcc": merchant_mcc[merchant],
                "amount": round(max(100.0, float(amount)), 0),
                "country": country,
                "device_token": device,
                "scenario": scenario,
                "Class": fraud,
            }
        )

    hour_p = np.array(LEGIT_HOURS) / sum(LEGIT_HOURS)

    # --- usuarios legitimos ----------------------------------------------------
    users = []
    for u in range(cfg.n_users):
        user = f"usr_{u:06d}"
        signup = 0 if rng.random() < 0.75 else int(rng.integers(0, cfg.days - 5))
        profile = {
            "user": user,
            "signup": signup,
            "mu": math.log(18_000) + rng.normal(0, 0.6),
            "rate": float(rng.gamma(2.0, 0.6)),
            "devices": [f"dev_{_short_id(rng)}"] + ([f"dev_{_short_id(rng)}"] if rng.random() < 0.3 else []),
            "favorites": list(rng.choice(merchants, size=int(rng.integers(8, 20)), replace=False, p=popularity)),
            "trip": None,
        }
        if rng.random() < 0.04:
            start = int(rng.integers(signup, cfg.days - 3))
            profile["trip"] = (start, start + int(rng.integers(3, 10)), str(rng.choice(FOREIGN)))
        users.append(profile)

        for day in range(signup, cfg.days):
            n = rng.poisson(profile["rate"])
            spree = rng.random() < 0.01
            base_hour = float(rng.choice(24, p=hour_p)) + rng.random()
            if spree:
                n += int(rng.integers(3, 6))
            for k in range(n):
                if spree:
                    hour = base_hour + k * rng.uniform(0.1, 0.5)
                else:
                    hour = float(rng.choice(24, p=hour_p)) + rng.random()
                merchant = (
                    str(rng.choice(profile["favorites"]))
                    if rng.random() < 0.85
                    else str(rng.choice(merchants, p=popularity))
                )
                amount = rng.lognormal(profile["mu"], 0.7)
                # Pasajes y recargas: montos chicos y legitimos. Sin esto, el
                # unico origen de montos bajo mil pesos seria la prueba de
                # tarjeta, y el modelo aprenderia que un pasaje de micro desde
                # un celular nuevo es fraude.
                if merchant_mcc[merchant] == "4111":
                    amount = rng.lognormal(math.log(900), 0.35)
                elif merchant_mcc[merchant] == "4814":
                    amount = rng.choice([1_000, 2_000, 3_000, 5_000, 10_000])
                if rng.random() < 0.02:  # compra grande ocasional
                    amount *= rng.uniform(3, 8)
                    merchant = str(rng.choice(risky)) if rng.random() < 0.5 else merchant
                if rng.random() < 0.002:  # cambio de celular: el nuevo queda conocido
                    profile["devices"].append(f"dev_{_short_id(rng)}")
                device = profile["devices"][-1] if rng.random() < 0.7 else str(rng.choice(profile["devices"]))
                trip = profile["trip"]
                country = trip[2] if trip and trip[0] <= day < trip[1] else "CL"
                add(_local(day, hour), profile["user"], merchant, amount, country, device, "legit", 0)

    def victim(min_day: int):
        while True:
            p = users[int(rng.integers(0, len(users)))]
            if p["signup"] + 3 <= cfg.days - 1 and max(p["signup"] + 3, min_day) < cfg.days:
                return p, int(rng.integers(max(p["signup"] + 3, min_day), cfg.days))

    # --- fraude: toma de cuenta -----------------------------------------------
    for _ in range(cfg.ato_events):
        p, day = victim(0)
        subtle = rng.random() < 0.25
        stolen_phone = rng.random() < 0.20
        device = str(rng.choice(p["devices"])) if stolen_phone else f"dev_{_short_id(rng)}"
        country = str(rng.choice(FOREIGN)) if (not subtle and rng.random() < 0.55) else "CL"
        night = rng.random() < (0.3 if subtle else 0.55)
        hour = rng.uniform(0, 6) if night else rng.uniform(7, 23)
        n_tx = int(rng.integers(1, 3)) if subtle else int(rng.integers(2, 7))
        ts = _local(day, hour)
        typical = math.exp(p["mu"])
        for _k in range(n_tx):
            merchant = str(rng.choice(hot)) if rng.random() < (0.35 if subtle else 0.65) else str(rng.choice(merchants, p=popularity))
            factor = rng.uniform(0.8, 2.0) if subtle else rng.uniform(2.0, 12.0)
            add(ts, p["user"], merchant, typical * factor, country, device, "ato", 1)
            ts += timedelta(minutes=float(rng.uniform(3, 25)))

    # --- fraude: prueba de tarjeta ----------------------------------------------
    for _ in range(cfg.card_testing_events):
        p, day = victim(0)
        device = f"dev_{_short_id(rng)}" if rng.random() < 0.6 else str(rng.choice(p["devices"]))
        country = str(rng.choice(FOREIGN)) if rng.random() < 0.3 else "CL"
        ts = _local(day, rng.uniform(0, 24))
        probes = int(rng.integers(6, 15))
        targets = rng.choice(risky + merchants[:40], size=probes, replace=False)
        for merchant in targets:
            add(ts, p["user"], str(merchant), rng.uniform(500, 3_000), country, device, "card_testing", 1)
            ts += timedelta(minutes=float(rng.uniform(0.5, 4)))
        for _k in range(int(rng.integers(1, 3))):
            add(ts, p["user"], str(rng.choice(hot)), math.exp(p["mu"]) * rng.uniform(5, 10), country, device, "card_testing", 1)
            ts += timedelta(minutes=float(rng.uniform(1, 6)))

    # --- fraude: cuentas nuevas -------------------------------------------------
    for f in range(cfg.fraud_accounts):
        user = f"usr_n{f:05d}"
        day = int(rng.integers(0, cfg.days - 3))
        device = f"dev_{_short_id(rng)}"
        country = "CL" if rng.random() < 0.7 else str(rng.choice(FOREIGN))
        ts = _local(day, rng.uniform(0, 24))
        for _k in range(int(rng.integers(3, 9))):
            merchant = str(rng.choice(hot)) if rng.random() < 0.6 else str(rng.choice(risky))
            add(ts, user, merchant, rng.lognormal(math.log(120_000), 0.6), country, device, "new_account", 1)
            ts += timedelta(hours=float(rng.uniform(0.2, 20)))

    frame = pd.DataFrame(rows).sort_values("ts", kind="stable").reset_index(drop=True)
    frame = frame[frame["ts"] < START + timedelta(days=cfg.days)].reset_index(drop=True)

    day_index = ((frame["ts"] - START).dt.total_seconds() // 86_400).astype(int)
    frame["period"] = np.where(
        day_index < cfg.reference_days,
        "reference",
        np.where(day_index >= cfg.days - cfg.test_days, "test", "train"),
    )

    # --- riesgo por comercio: solo con el periodo de referencia ---------------
    ref = frame[frame["period"] == "reference"]
    prior = float(ref["Class"].mean())
    smoothing = 50.0
    grouped = ref.groupby("merchant_id")["Class"].agg(["sum", "count"])
    table = {
        merchant: round(float((row["sum"] + prior * smoothing) / (row["count"] + smoothing)), 6)
        for merchant, row in grouped.iterrows()
    }
    merchant_risk = MerchantRisk(table, prior=round(prior, 6))

    # --- features: la misma ruta que la API -------------------------------------
    logger.info("Reproduciendo %d transacciones por FeatureAssembler", len(frame))
    state = MemoryFeatureState()
    names = Settings().feature_names
    feature_rows = []
    for rec in frame.itertuples(index=False):
        request = ScoreRequest.model_construct(
            id_tx=rec.id_tx,
            user_token=rec.user_token,
            merchant_id=rec.merchant_id,
            amount=float(rec.amount),
            currency="CLP",
            country=rec.country,
            device_token=rec.device_token,
            mcc=rec.mcc,
            timestamp=rec.ts.to_pydatetime(),
        )
        values = compute_features(request, state.read_and_update(request), merchant_risk)
        feature_rows.append([values[n] for n in names])

    features = pd.DataFrame(feature_rows, columns=names)
    out = pd.concat([frame.reset_index(drop=True), features], axis=1)
    return out, merchant_risk, {"prior": prior, "smoothing": smoothing, "table": table}
