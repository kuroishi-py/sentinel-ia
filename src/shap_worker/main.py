"""shap-worker: explicaciones fuera de la ruta critica (EP2 seccion 5.1).

Consume ``decision.v1`` desde RabbitMQ y, para cada rechazo o revision, calcula
la contribucion de cada feature al score con TreeSHAP y guarda el caso.

Por que vive aparte de la API: calcular SHAP toma decenas de milisegundos, y el
pago no puede esperar eso (RNF1). El rechazo se devuelve al instante y la
explicacion se construye despues, antes de que un analista abra el caso.

Esto es, al mismo tiempo, el consumidor que la pauta de la EP3 pide para
demostrar "comunicacion entre servicios distribuidos".

Uso:
    python -m src.shap_worker.main
"""

from __future__ import annotations

import json
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.decision_api.config import get_settings  # noqa: E402
from src.decision_api.schemas import Decision, Explanation  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"shap_worker","msg":"%(message)s"}',
)
logger = logging.getLogger("shap_worker")

QUEUE_NAME = "shap.cases"
#: Donde se guardan los casos. En produccion esto es DynamoDB (EP2 5.1); en
#: local, un JSONL que el dashboard lee directo.
CASES_PATH = Path("data/cases.jsonl")


class ShapExplainer:
    """TreeSHAP sobre el modelo local.

    Si el modelo o shap no estan disponibles, devuelve None en vez de caerse:
    un caso sin explicacion sigue siendo revisable por una persona.
    """

    def __init__(self, settings) -> None:
        self._settings = settings
        self._explainer = None
        self._feature_names = settings.feature_names
        self._load()

    def _load(self) -> None:
        model_path = Path(self._settings.model_path)
        if not model_path.exists():
            logger.warning("Sin modelo en %s: los casos iran sin explicacion", model_path)
            return
        try:
            import shap
            import xgboost as xgb
        except ImportError as exc:
            logger.warning("Falta una dependencia para SHAP (%s)", exc)
            return

        booster = xgb.Booster()
        booster.load_model(str(model_path))
        booster.set_param({"nthread": 1})  # mismo motivo que en scorer.py
        self._explainer = shap.TreeExplainer(booster)
        logger.info("TreeExplainer listo")

    def explain(self, id_tx: str, features: dict[str, float]) -> Explanation | None:
        if self._explainer is None:
            return None
        import numpy as np

        vector = np.asarray(
            [[float(features.get(name, 0.0)) for name in self._feature_names]],
            dtype=np.float32,
        )
        values = self._explainer.shap_values(vector)[0]
        contributions = {
            name: round(float(value), 6) for name, value in zip(self._feature_names, values, strict=False)
        }
        # Lo que el analista mira primero: las tres features que mas empujaron.
        top = sorted(contributions, key=lambda k: abs(contributions[k]), reverse=True)[:3]
        return Explanation(
            id_tx=id_tx,
            base_value=round(float(self._explainer.expected_value), 6),
            contributions=contributions,
            top_features=top,
            explained_at=datetime.now(UTC),
        )


def store_case(event: dict, explanation: Explanation | None) -> None:
    """Crea el caso para Fraud Ops (RF6)."""
    CASES_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id_tx": event.get("id_tx"),
        "decision": event.get("decision"),
        "score": event.get("score"),
        "amount": event.get("amount"),
        "merchant_id": event.get("merchant_id"),
        "model_version": event.get("model_version"),
        "reason_codes": event.get("reason_codes", []),
        "degraded": event.get("degraded", False),
        # El valor de cada feature al decidir: la explicacion dice cuanto peso,
        # esto dice cuanto valia (por ejemplo, 4 compras en la ultima hora).
        "features": event.get("features", {}),
        "decided_at": event.get("decided_at"),
        "explanation": explanation.model_dump(mode="json") if explanation else None,
        "status": "OPEN",
        "created_at": datetime.now(UTC).isoformat(),
    }
    with CASES_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    import pika

    settings = get_settings()
    explainer = ShapExplainer(settings)

    params = pika.URLParameters(settings.amqp_url)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.exchange_declare(
        exchange=settings.decision_exchange, exchange_type="topic", durable=True
    )
    channel.queue_declare(queue=QUEUE_NAME, durable=True)
    channel.queue_bind(exchange=settings.decision_exchange, queue=QUEUE_NAME, routing_key="#")
    channel.basic_qos(prefetch_count=20)

    logger.info("Escuchando %s en %s", settings.decision_exchange, params.host)

    def on_message(ch, method, properties, body) -> None:
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            logger.error("Mensaje ilegible, se descarta")
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        decision = event.get("decision")
        # Solo se explica lo que una persona va a revisar (RF4).
        if decision in {Decision.REJECT.value, Decision.REVIEW.value}:
            explanation = explainer.explain(event.get("id_tx", ""), event.get("features", {}))
            store_case(event, explanation)
            top = ", ".join(explanation.top_features) if explanation else "sin explicacion"
            logger.info("Caso %s (%s): %s", event.get("id_tx"), decision, top)

        ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_consume(queue=QUEUE_NAME, on_message_callback=on_message)

    def shutdown(signum, frame) -> None:  # noqa: ARG001
        logger.info("Cerrando")
        try:
            channel.stop_consuming()
            connection.close()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    channel.start_consuming()


if __name__ == "__main__":
    main()
