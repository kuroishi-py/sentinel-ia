"""Entrenamiento del modelo de scoring.

Entrena un XGBoost y deja en la carpeta de salida el modelo, su SHA-256, un
``metadata.json`` con hiperparametros y dataset, y un ``metrics.json`` con las
metricas que pide la pauta de la EP3 (punto 2.3).

Hay dos modelos, y es a proposito (issue #1, opcion C):

- **Modelo de servicio** (``--simulated``): las 11 features del diseno, con
  nombre, calculadas por el mismo codigo que usa la API. Es el que sirve
  ``/v1/score`` y el que explica SHAP en el dashboard. Se entrena sobre la
  historia simulada de ``simulate.py``, con particion temporal.
- **Benchmark real** (``--data``): el dataset de la ULB, con componentes PCA.
  Da las metricas sobre fraude real del informe, pero no se puede servir: la
  pasarela no envia componentes principales.

Sobre las metricas: con 0,17% de fraude, un modelo que apruebe todo logra 99,8%
de accuracy. Por eso el informe debe reportar precision, recall, F1 y PR-AUC, y
amarrarlos al objetivo de la EP1: bajar del 40% de falsos positivos del motor de
reglas sin perder recall. Accuracy se calcula igual, pero solo para dejar por
escrito por que no se usa.

Uso:
    # modelo de servicio: deja artifacts/model.json y artifacts/merchant_risk.json
    python -m src.training.train --simulated

    # benchmark con datos reales de la ULB, en su propia carpeta
    python -m src.training.train --data data/creditcard.csv --label Class \\
        --drop Time --out artifacts/benchmark_ulb

    # datos sinteticos sin historia, solo para probar que el pipeline corre
    python -m src.training.train --synthetic
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
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

#: Restricciones monotonicas del modelo de servicio (EP2 seccion 8.2, control
#: contra evasion AML.T0015): mas velocidad, mas monto sobre el promedio o un
#: dispositivo nuevo nunca pueden *bajar* el score. Sin esto, un defraudador
#: que sondea la API podria encontrar zonas donde «mas sospechoso» puntua menos.
#: 1 = creciente, -1 = decreciente, 0 = libre.
MONOTONE = {
    "amount_log": 0,
    "amount_vs_user_avg": 1,
    "tx_count_1h": 1,
    "tx_count_24h": 1,
    "amount_sum_24h": 0,
    "distinct_merchants_24h": 1,
    "user_age_days": -1,
    "merchant_risk": 1,
    "is_foreign_country": 1,
    "hour_of_day": 0,
    "is_new_device": 1,
}

#: Que significa cada feature, para el dashboard y el informe.
FEATURE_DESCRIPTIONS = {
    "amount_log": "Monto de la transaccion (escala logaritmica)",
    "amount_vs_user_avg": "Monto dividido por el promedio historico del usuario",
    "tx_count_1h": "Transacciones del usuario en la ultima hora",
    "tx_count_24h": "Transacciones del usuario en las ultimas 24 h",
    "amount_sum_24h": "Monto acumulado del usuario en las ultimas 24 h",
    "distinct_merchants_24h": "Comercios distintos del usuario en las ultimas 24 h",
    "user_age_days": "Dias desde la primera transaccion del usuario",
    "merchant_risk": "Tasa historica de fraude del comercio",
    "is_foreign_country": "Compra fuera de Chile",
    "hour_of_day": "Hora del dia (UTC)",
    "is_new_device": "Dispositivo visto por primera vez hace menos de 24 h",
}


def build_synthetic(n_rows: int = 60_000, fraud_rate: float = 0.005, seed: int = 42):
    """Dataset sintetico con el desbalance del caso (0,5% de fraude).

    No sustituye al dataset real para el informe ni al simulado para el
    servicio: cada feature se sortea por separado, sin historia de usuario.
    Sirve para verificar que el pipeline corre.
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


def baseline_rules(frame) -> dict:
    """Un motor de reglas tipico, sobre los mismos datos de prueba.

    Es la referencia contra la que se mide el modelo: el caso parte de un motor
    de reglas con 40% de falsos positivos. Estas son las reglas obvias (monto
    alto, extranjero, dispositivo nuevo, rafaga, madrugada) y alertan si se
    cumple cualquiera.
    """
    night_utc = frame["hour_of_day"].between(3, 8)  # 0 a 5 h en Chile
    flagged = (
        (frame["amount_vs_user_avg"] > 3)
        | (frame["is_foreign_country"] == 1)
        | (frame["is_new_device"] == 1)
        | (frame["tx_count_1h"] >= 3)
        | night_utc
    ).astype(int)
    y = frame["Class"].values
    pred = flagged.values
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    return {
        "rules": [
            "amount_vs_user_avg > 3",
            "is_foreign_country = 1",
            "is_new_device = 1",
            "tx_count_1h >= 3",
            "madrugada en Chile (0 a 5 h)",
        ],
        "precision": round(tp / (tp + fp), 6) if tp + fp else 0.0,
        "recall": round(tp / (tp + fn), 6) if tp + fn else 0.0,
        "false_positive_rate_on_legit": round(fp / (fp + tn), 6) if fp + tn else 0.0,
        "share_of_alerts_that_are_false": round(fp / (tp + fp), 6) if tp + fp else 0.0,
        "alerts": tp + fp,
        "confusion_matrix": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
    }


def global_importance(booster, X, features: list[str], sample: int = 5_000) -> dict[str, float]:
    """Importancia global: promedio de |SHAP| por feature sobre una muestra."""
    try:
        import numpy as np
        import shap
    except ImportError:
        return {}
    rows = X.sample(n=min(sample, len(X)), random_state=42)
    values = shap.TreeExplainer(booster).shap_values(rows.values)
    mean_abs = np.abs(values).mean(axis=0)
    ranked = sorted(zip(features, mean_abs, strict=True), key=lambda kv: kv[1], reverse=True)
    return {name: round(float(v), 6) for name, v in ranked}


def write_artifacts(out: Path, booster, metadata: dict, metrics: dict) -> str:
    """Modelo, hash, metadata y metricas en ``out``. Devuelve el SHA-256."""
    out.mkdir(parents=True, exist_ok=True)
    model_path = out / "model.json"
    booster.save_model(str(model_path))
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    # newline="\n": en Windows write_text escribe CRLF por omision, y los
    # artefactos tienen que salir iguales en cualquier plataforma.
    (out / "model.json.sha256").write_text(f"{digest}  model.json\n", encoding="utf-8", newline="\n")
    metadata["sha256"] = digest
    (out / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    (out / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Entrena el modelo de SENTINEL-IA")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--data", help="CSV de entrenamiento (benchmark con datos reales)")
    source.add_argument("--simulated", action="store_true", help="Modelo de servicio sobre la historia simulada")
    source.add_argument("--synthetic", action="store_true", help="Datos sinteticos sin historia, solo para probar")
    parser.add_argument("--label", default="Class", help="Columna objetivo")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--out", default=str(ARTIFACTS), help="Carpeta de salida (por omision artifacts/)")
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
    parser.add_argument("--threshold-reject", type=float, default=0.80, help="Umbral de las metricas principales")
    parser.add_argument("--threshold-review", type=float, default=0.45, help="Umbral de las metricas de revision")
    args = parser.parse_args()

    import pandas as pd
    import xgboost as xgb
    from sklearn.model_selection import train_test_split

    out = Path(args.out)
    extra_metadata: dict = {}
    test_frame = None

    if args.simulated:
        from src.decision_api.config import Settings

        from .simulate import SimulationConfig, simulate

        config = SimulationConfig()
        logger.info("Simulando %d usuarios por %d dias", config.n_users, config.days)
        frame, _risk, risk_meta = simulate(config)

        features = Settings().feature_names
        train_frame = frame[frame["period"] == "train"]
        test_frame = frame[frame["period"] == "test"]
        # Validacion para la parada temprana: la ultima semana del periodo de
        # entrenamiento. El conjunto de prueba no se mira hasta el final.
        cut = train_frame["ts"].max().to_pydatetime() - timedelta(days=7)
        fit_frame = train_frame[train_frame["ts"] <= cut]
        val_frame = train_frame[train_frame["ts"] > cut]
        X_fit, y_fit = fit_frame[features].astype("float32"), fit_frame[args.label].astype(int)
        X_val, y_val = val_frame[features].astype("float32"), val_frame[args.label].astype(int)
        X_test, y_test = test_frame[features].astype("float32"), test_frame[args.label].astype(int)
        rows, positives = len(frame), int(frame[args.label].sum())

        out.mkdir(parents=True, exist_ok=True)
        risk_doc = {
            "description": (
                "Tasa de fraude por comercio con suavizado bayesiano, calculada solo con "
                "el periodo de referencia (anterior al de entrenamiento)."
            ),
            "prior": round(risk_meta["prior"], 6),
            "smoothing": risk_meta["smoothing"],
            "merchants": risk_meta["table"],
        }
        (out / "merchant_risk.json").write_text(
            json.dumps(risk_doc, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        extra_metadata = {
            "dataset": "simulado (src/training/simulate.py)",
            "simulation": {
                "seed": config.seed,
                "users": config.n_users,
                "merchants": config.n_merchants,
                "days": config.days,
                "split": {
                    "reference_days": config.reference_days,
                    "train_days": config.days - config.reference_days - config.test_days,
                    "test_days": config.test_days,
                    "validation": "ultimos 7 dias del periodo de entrenamiento",
                },
                "rows_by_period": {k: int(v) for k, v in frame["period"].value_counts().items()},
                "fraud_by_scenario": {
                    k: int(v) for k, v in frame[frame["Class"] == 1]["scenario"].value_counts().items()
                },
            },
            "feature_descriptions": FEATURE_DESCRIPTIONS,
            "monotone_constraints": MONOTONE,
        }
    else:
        if args.synthetic or not args.data:
            logger.info("Generando dataset sintetico (no sirve para el informe final)")
            frame = build_synthetic()
        else:
            path = Path(args.data)
            if not path.exists():
                raise SystemExit(
                    f"No existe {path}. Baja el dataset con python -m src.training.download_dataset, "
                    f"o usa --simulated para el modelo de servicio."
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
        rows, positives = len(frame), int(y.sum())
        if positives < 10:
            raise SystemExit("Hay menos de 10 casos de fraude: el modelo no puede aprender nada.")
        X_fit, X_test, y_fit, y_test = train_test_split(X, y, test_size=0.2, stratify=y, random_state=42)
        # Como en la primera version: la parada temprana mira el conjunto de
        # prueba. Con 492 fraudes no alcanza para separar una validacion.
        X_val, y_val = X_test, y_test
        extra_metadata = {"dataset": "sintetico" if (args.synthetic or not args.data) else str(args.data)}

    logger.info("Filas: %d | features: %d | fraude: %d (%.3f%%)", rows, len(features), positives, 100 * positives / rows)

    params = dict(DEFAULT_PARAMS)
    # Con 0,5% de fraude, sin este peso el modelo aprende a decir "no" siempre.
    params["scale_pos_weight"] = float((y_fit == 0).sum() / max(1, (y_fit == 1).sum()))
    if args.simulated:
        params["monotone_constraints"] = "(" + ",".join(str(MONOTONE[f]) for f in features) + ")"
    logger.info("scale_pos_weight = %.1f", params["scale_pos_weight"])

    dfit = xgb.DMatrix(X_fit, label=y_fit, feature_names=features)
    dval = xgb.DMatrix(X_val, label=y_val, feature_names=features)
    dtest = xgb.DMatrix(X_test, label=y_test, feature_names=features)

    booster = xgb.train(
        params,
        dfit,
        num_boost_round=args.rounds,
        evals=[(dfit, "train"), (dval, "valid")],
        early_stopping_rounds=30,
        verbose_eval=50,
    )
    # Se guarda solo hasta la mejor iteracion: lo que viene despues sobreajusta.
    booster = booster[: booster.best_iteration + 1]

    y_prob = booster.predict(dtest)
    metrics = {
        "at_reject_threshold": evaluate(y_test.values, y_prob, args.threshold_reject),
        "at_review_threshold": evaluate(y_test.values, y_prob, args.threshold_review),
        "sweep": [evaluate(y_test.values, y_prob, t) for t in (0.1, 0.3, 0.45, 0.5, 0.7, 0.8, 0.9)],
        "test_rows": int(len(y_test)),
        "test_frauds": int(y_test.sum()),
    }

    if test_frame is not None:
        flagged = y_prob >= args.threshold_review
        by_scenario = {}
        for scenario, group in test_frame.assign(flagged=flagged).groupby("scenario"):
            if scenario == "legit":
                continue
            by_scenario[str(scenario)] = {
                "frauds": int(len(group)),
                "recall_at_review": round(float(group["flagged"].mean()), 6),
            }
        metrics["recall_by_scenario_at_review"] = by_scenario
        metrics["baseline_rules"] = baseline_rules(test_frame)
        metrics["global_importance_mean_abs_shap"] = global_importance(booster, X_test, features)

    version = args.model_version or f"v{datetime.now(UTC):%Y%m%d.%H%M}"
    metadata = {
        "model_version": version,
        "trained_at": datetime.now(UTC).isoformat(),
        "framework": f"xgboost {xgb.__version__}",
        "algorithm": "gradient boosting (binary:logistic)",
        "rows": int(rows),
        "fraud_rate": round(float(positives / rows), 6),
        "features": features,
        "hyperparameters": params,
        "num_boost_round": int(booster.num_boosted_rounds()),
        **extra_metadata,
    }
    digest = write_artifacts(out, booster, metadata, metrics)

    main_metrics = metrics["at_reject_threshold"]
    logger.info("Modelo %s guardado en %s", version, out / "model.json")
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
    if "baseline_rules" in metrics:
        base = metrics["baseline_rules"]
        logger.info(
            "Motor de reglas sobre los mismos datos: precision=%.4f recall=%.4f FP sobre legitimas=%.4f",
            base["precision"],
            base["recall"],
            base["false_positive_rate_on_legit"],
        )


if __name__ == "__main__":
    main()
