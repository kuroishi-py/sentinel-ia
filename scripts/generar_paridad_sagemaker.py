"""Transacciones de referencia para comprobar el endpoint de SageMaker.

Reproduce los tres escenarios de demostracion (compra normal, toma de cuenta y
prueba de tarjeta) con el mismo FeatureAssembler de la API y guarda, para la
ultima transaccion de cada uno, el vector de 11 features y el score que da el
modelo versionado en memoria.

``src/training/sagemaker_deploy.py`` envia esos mismos vectores al endpoint y
compara: si el endpoint sirve exactamente el modelo de la API, los scores
coinciden hasta el redondeo. Es la evidencia de que los dos backends de la
decision D2 son el mismo modelo, y lo unico que cambia es donde corre.

Uso (desde la raiz del repositorio):
    python scripts/generar_paridad_sagemaker.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.decision_api.config import Settings  # noqa: E402
from src.decision_api.features import FeatureAssembler, MemoryFeatureState, MerchantRisk  # noqa: E402
from src.decision_api.schemas import ScoreRequest  # noqa: E402
from src.decision_api.scorer import ModelLoader  # noqa: E402
from src.demo import scenarios  # noqa: E402

ARTIFACTS = ROOT / "artifacts"
OUT = ARTIFACTS / "sagemaker_parity.json"
#: Fecha fija: el archivo sale igual cada vez que se genera.
NOW = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)


def main() -> None:
    settings = Settings(model_path=str(ARTIFACTS / "model.json"), scorer_backend="local")
    scorer = ModelLoader(settings).load()
    risk = MerchantRisk.from_file(ARTIFACTS / "merchant_risk.json")
    table = json.loads((ARTIFACTS / "merchant_risk.json").read_text(encoding="utf-8")).get("merchants", {})
    risky = max(table, key=table.get) if table else "mrc_0001"

    cases = {
        "compra_normal": lambda u: [scenarios.normal_purchase(u, NOW)],
        "toma_de_cuenta": lambda u: scenarios.account_takeover(u, risky, NOW),
        "prueba_de_tarjeta": lambda u: scenarios.card_testing(u, NOW),
    }

    rows = []
    for name, make in cases.items():
        assembler = FeatureAssembler(store=MemoryFeatureState(), merchant_risk=risk)
        user = f"usr_paridad_{name}"
        feature_set = None
        for tx in [*scenarios.normal_history(user, NOW), *make(user)]:
            feature_set = assembler.build(ScoreRequest(**tx))
        vector = feature_set.as_vector(settings.feature_names)
        rows.append(
            {
                "escenario": name,
                "features": dict(zip(settings.feature_names, vector, strict=True)),
                "vector": vector,
                "score_api": round(float(scorer.score(vector)), 6),
            }
        )

    payload = {
        "model_version": scorer.model_version,
        "feature_names": settings.feature_names,
        "casos": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for row in rows:
        print(f"{row['escenario']:<18} score_api={row['score_api']:.6f}")
    print(f"Guardado en {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
