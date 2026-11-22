"""Handler de inferencia para el endpoint de SageMaker.

El contenedor XGBoost de AWS llama a estas cuatro funciones. Se escriben a mano
para controlar dos cosas que importan en este caso:

1. El orden de las features. El endpoint recibe un CSV sin nombres de columna,
   asi que el orden es el contrato. Si entrenamiento e inferencia no coinciden,
   el modelo lee el monto como si fuera la hora y nadie se da cuenta
   (restriccion RT2, feature parity).
2. La forma de la respuesta. ``SageMakerScorer`` en ``scorer.py`` espera una
   probabilidad por linea, en texto plano.
"""

from __future__ import annotations

import json
import os

import numpy as np
import xgboost as xgb

#: Mismo orden que Settings.feature_names en src/decision_api/config.py.
FEATURE_NAMES = [
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


def model_fn(model_dir: str):
    """Carga el modelo al iniciar el contenedor."""
    booster = xgb.Booster()
    for name in ("model.json", "xgboost-model", "model.bst"):
        path = os.path.join(model_dir, name)
        if os.path.exists(path):
            booster.load_model(path)
            return booster
    raise FileNotFoundError(f"No se encontro el modelo en {model_dir}")


def input_fn(request_body, request_content_type: str):
    """Convierte el cuerpo de la solicitud en una matriz de features."""
    if request_content_type == "text/csv":
        if isinstance(request_body, bytes):
            request_body = request_body.decode("utf-8")
        rows = [
            [float(value) for value in line.split(",")]
            for line in request_body.strip().splitlines()
            if line.strip()
        ]
        return np.asarray(rows, dtype=np.float32)

    if request_content_type == "application/json":
        if isinstance(request_body, bytes):
            request_body = request_body.decode("utf-8")
        payload = json.loads(request_body)
        records = payload if isinstance(payload, list) else [payload]
        # Con JSON se aceptan nombres, y el orden lo pone FEATURE_NAMES.
        return np.asarray(
            [[float(record.get(name, 0.0)) for name in FEATURE_NAMES] for record in records],
            dtype=np.float32,
        )

    raise ValueError(f"Content-Type no soportado: {request_content_type}")


def predict_fn(input_data, model):
    """Calcula la probabilidad de fraude.

    inplace_predict evita construir un DMatrix por solicitud, que es la
    optimizacion que la EP2 anoto en la seccion 7.4.
    """
    return model.inplace_predict(input_data)


def output_fn(prediction, accept: str) -> tuple[str, str]:
    """Serializa la respuesta."""
    values = np.asarray(prediction).ravel()
    if accept == "application/json":
        return json.dumps({"scores": [float(v) for v in values]}), accept
    return "\n".join(f"{float(v):.6f}" for v in values), "text/csv"
