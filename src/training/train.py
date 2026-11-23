"""Entrenamiento del modelo de scoring.

Entrena un XGBoost sobre datos de transacciones y deja tres cosas en
``artifacts/``: el modelo, su SHA-256 y un ``metrics.json`` con las metricas que
pide la pauta de la EP3 (punto 2.3).

Sobre las metricas: con 0,17% de fraude, un modelo que apruebe todo logra 99,8%
de accuracy. Por eso el informe debe reportar precision, recall, F1 y PR-AUC, y
amarrarlos al objetivo de la EP1: bajar del 40% de falsos positivos del motor de
reglas sin perder recall. Accuracy se calcula igual, pero solo para dejar por
escrito por que no se usa.

Uso:
    # dataset real (recomendado: creditcard.csv de la ULB)
    python -m src.training.train --data data/creditcard.csv --label Class

    # datos sinteticos, para probar que el pipeline corre de punta a punta
    python -m src.training.train --synthetic
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("train")

ARTIFACTS = Path("artifacts")

#: Hiperparametros de partida. Quedan registrados en metadata.json para que el
#: entrenamiento sea reproducible (restriccion RT2 y convencion C3).
DEFAULT_PARAMS = {
    "objective": "binary:logistic",
    "eval_metric": ["aucpr", "logloss"],
    "max_depth": 6,
    "eta": 0.1,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 5,
    "tree_method": "hist",
    "seed": 42,
}
DEFAULT_ROUNDS = 300


def build_synthetic(n_rows: int = 60_000, fraud_rate: float = 0.005, seed: int = 42):
    """Dataset sintetico con el desbalance del caso (0,5% de fraude).

    No sustituye al dataset real para el informe: sirve para verificar que el
    pipeline completo corre antes de bajar 150 MB de datos.
    """
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    n_fraud = int(n_rows * fraud_rate)
    n_legit = n_rows - n_fraud

    def block(n: int, fraud: bool) -> dict:
        shift = 1.0 if fraud else 0.0
        return {
            "amount_log": rng.normal(9.5 + shift * 1.8, 1.2, n),
            "amount_vs_user_avg": rng.lognormal(shift * 0.9, 0.6, n),
            "tx_count_1h": rng.poisson(1 + shift * 4, n).astype(float),
            "tx_count_24h": rng.poisson(5 + shift * 9, n).astype(float),
            "amount_sum_24h": rng.lognormal(10 + shift, 1.0, n),
            "distinct_merchants_24h": rng.poisson(2 + shift * 3, n).astype(float),
            "user_age_days": rng.integers(1, 2000, n).astype(float) * (0.3 if fraud else 1.0),
            "merchant_risk": rng.beta(1 + shift * 3, 8, n),
            "is_foreign_country": rng.binomial(1, 0.05 + shift * 0.35, n).astype(float),
            "hour_of_day": rng.integers(0, 24, n).astype(float),
            "is_new_device": rng.binomial(1, 0.1 + shift * 0.5, n).astype(float),
        }

    legit = pd.DataFrame(block(n_legit, fraud=False))
    legit["Class"] = 0
    fraud = pd.DataFrame(block(n_fraud, fraud=True))
    fraud["Class"] = 1

    frame = pd.concat([legit, fraud], ignore_index=True)
    return frame.sample(frac=1.0, random_state=seed).reset_index(drop=True)


def evaluate(y_true, y_prob, threshold: float) -> dict:
    """Metricas de clasificacion a un umbral dado."""
    from sklearn.metrics import (
        accuracy_score,
        average_precision_score,
        confusion_matrix,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    y_pred = (y_prob >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()

    return {
        "threshold": round(float(threshold), 4),
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 6),
        "precision": round(float(precision_score(y_true, y_pred, zero_division=0)), 6),
        "recall": round(float(recall_score(y_true, y_pred, zero_division=0)), 6),
        "f1": round(float(f1_score(y_true, y_pred, zero_division=0)), 6),
        "pr_auc": round(float(average_precision_score(y_true, y_prob)), 6),
        "roc_auc": round(float(roc_auc_score(y_true, y_prob)), 6),
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
        # Esta es la metrica de negocio: el motor de reglas actual esta en 40%.
        "false_positive_rate_on_legit": round(float(fp / (fp + tn)) if (fp + tn) else 0.0, 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Entrena el modelo de SENTINEL-IA")
    parser.add_argument("--data", help="CSV de entrenamiento")
    parser.add_argument("--label", default="Class", help="Columna objetivo")
    parser.add_argument("--synthetic", action="store_true", help="Genera datos de prueba")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument(
        "--drop",
        nargs="*",
        default=[],
        metavar="COLUMNA",
        help=(
            "Columnas a excluir. Para el dataset de la ULB conviene --drop Time: "
            "son segundos desde la primera transaccion y el archivo cubre dos dias, "
            "asi que el modelo aprenderia el orden de ese archivo, no a detectar fraude."
        ),
    )
    parser.add_argument("--model-version", default=None, help="Por omision, la fecha y hora")
    parser.add_argument(
        "--threshold-reject",
        type=float,
        default=0.80,
        help="Umbral con el que se reportan las metricas principales",
    )
    args = parser.parse_args()

    import pandas as pd
    import xgboost as xgb
    from sklearn.model_selection import train_test_split

    if args.synthetic or not args.data:
        logger.info("Generando dataset sintetico (no sirve para el informe final)")
        frame = build_synthetic()
    else:
        path = Path(args.data)
        if not path.exists():
            raise SystemExit(
                f"No existe {path}. Baja el dataset (por ejemplo creditcard.csv de la ULB) "
                f"a data/, o usa --synthetic para probar el pipeline."
            )
        logger.info("Leyendo %s", path)
        frame = pd.read_csv(path)

    if args.label not in frame.columns:
        raise SystemExit(f"La columna '{args.label}' no esta en el CSV. Columnas: {list(frame.columns)}")

    missing_drop = [c for c in args.drop if c not in frame.columns]
    if missing_drop:
        raise SystemExit(f"--drop menciona columnas que no existen: {missing_drop}")

    features = [c for c in frame.columns if c != args.label and c not in args.drop]
    if args.drop:
        logger.info("Excluidas por --drop: %s", ", ".join(args.drop))
    X = frame[features].astype("float32")
    y = frame[args.label].astype(int)

    positives = int(y.sum())
    logger.info("Filas: %d | features: %d | fraude: %d (%.3f%%)", len(frame), len(features), positives, 100 * positives / len(frame))
    if positives < 10:
        raise SystemExit("Hay menos de 10 casos de fraude: el modelo no puede aprender nada.")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=42
    )

    params = dict(DEFAULT_PARAMS)
    # Con 0,5% de fraude, sin este peso el modelo aprende a decir "no" siempre.
    params["scale_pos_weight"] = float((y_train == 0).sum() / max(1, (y_train == 1).sum()))
    logger.info("scale_pos_weight = %.1f", params["scale_pos_weight"])

    dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=features)
    dtest = xgb.DMatrix(X_test, label=y_test, feature_names=features)

    booster = xgb.train(
        params,
        dtrain,
        num_boost_round=args.rounds,
        evals=[(dtrain, "train"), (dtest, "test")],
        early_stopping_rounds=30,
        verbose_eval=50,
    )

    y_prob = booster.predict(dtest)
    metrics = {
        "at_reject_threshold": evaluate(y_test.values, y_prob, args.threshold_reject),
        "at_review_threshold": evaluate(y_test.values, y_prob, 0.45),
        "sweep": [evaluate(y_test.values, y_prob, t) for t in (0.3, 0.5, 0.7, 0.9)],
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    model_path = ARTIFACTS / "model.json"
    booster.save_model(str(model_path))

    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    (ARTIFACTS / "model.json.sha256").write_text(f"{digest}  model.json\n", encoding="utf-8")

    version = args.model_version or f"v{datetime.now(UTC):%Y%m%d.%H%M}"
    metadata = {
        "model_version": version,
        "trained_at": datetime.now(UTC).isoformat(),
        "framework": f"xgboost {xgb.__version__}",
        "algorithm": "gradient boosting (binary:logistic)",
        "rows": int(len(frame)),
        "fraud_rate": round(float(positives / len(frame)), 6),
        "features": features,
        "hyperparameters": params,
        "num_boost_round": int(booster.best_iteration or args.rounds),
        "sha256": digest,
        "dataset": "sintetico" if (args.synthetic or not args.data) else str(args.data),
    }
    (ARTIFACTS / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (ARTIFACTS / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    main_metrics = metrics["at_reject_threshold"]
    logger.info("Modelo %s guardado en %s", version, model_path)
    logger.info("SHA-256 %s", digest[:16])
    logger.info(
        "precision=%.4f recall=%.4f f1=%.4f pr_auc=%.4f | falsos positivos sobre legitimas=%.4f",
        main_metrics["precision"],
        main_metrics["recall"],
        main_metrics["f1"],
        main_metrics["pr_auc"],
        main_metrics["false_positive_rate_on_legit"],
    )
    logger.info("Accuracy %.6f (se reporta solo para mostrar por que no sirve aqui)", main_metrics["accuracy"])


if __name__ == "__main__":
    main()
