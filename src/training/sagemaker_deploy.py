"""Entrenamiento y despliegue en SageMaker.

Este archivo es el que resuelve el choque entre la decision D2 de la EP2 (el
modelo vive dentro de la API, en ECS, para ahorrar un salto de red) y la pauta
de la EP3, que exige "despliegue en AWS SageMaker" con "endpoints configurados".

La salida es un endpoint Serverless Inference: escala solo, no cobra instancia
encendida y da capturas, logs y metricas de CloudWatch que son evidencia
directa para los indicadores IE4 e IE5.

Con el endpoint arriba, ``SCORER_BACKEND=sagemaker`` hace que decision-api lo
invoque, y la misma prueba de carga (``load/locustfile.py``) se corre contra los
dos backends. La diferencia de p99 entre ambos deja de ser un supuesto del
diseno y pasa a ser un resultado medido, que es lo que la pauta pide justificar.

Uso:
    python -m src.training.sagemaker_deploy --role-arn arn:aws:iam::123:role/SageMakerRole
    python -m src.training.sagemaker_deploy --role-arn ... --skip-training

Requiere ``pip install sagemaker boto3`` y credenciales de AWS configuradas.
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sagemaker-deploy")

#: sa-east-1 es la region del caso (EP2 seccion 3.2).
DEFAULT_REGION = "sa-east-1"
#: Instancia de entrenamiento. La EP2 dimensiona ml.m5.12xlarge para 200M
#: filas; para el dataset de la evaluacion basta una fraccion de eso.
DEFAULT_TRAIN_INSTANCE = "ml.m5.xlarge"


def upload_dataset(session, bucket: str, prefix: str, local_csv: str) -> str:
    logger.info("Subiendo %s a s3://%s/%s/", local_csv, bucket, prefix)
    return session.upload_data(path=local_csv, bucket=bucket, key_prefix=f"{prefix}/train")


def train(role_arn: str, region: str, train_s3: str, instance: str):
    """Training job con el contenedor XGBoost administrado de AWS."""
    import sagemaker
    from sagemaker.inputs import TrainingInput
    from sagemaker.xgboost.estimator import XGBoost

    session = sagemaker.Session()
    estimator = XGBoost(
        entry_point="train.py",
        source_dir="src/training",
        role=role_arn,
        instance_count=1,
        instance_type=instance,
        framework_version="1.7-1",
        py_version="py3",
        output_path=f"s3://{session.default_bucket()}/sentinel-ia/output",
        hyperparameters={"rounds": 300},
        sagemaker_session=session,
    )
    logger.info("Lanzando training job en %s", instance)
    estimator.fit({"train": TrainingInput(train_s3, content_type="text/csv")})
    logger.info("Modelo entrenado: %s", estimator.model_data)
    return estimator


def deploy_serverless(model, endpoint_name: str, memory_mb: int = 2048, concurrency: int = 20):
    """Endpoint Serverless Inference.

    memory_mb 2048 va alineado con la tarea de 2 GB que dimensiona la EP2
    (seccion 7.2). max_concurrency 20 cubre con holgura el pico de 116 tx/s del
    caso, considerando respuestas de pocas decenas de milisegundos.
    """
    from sagemaker.serverless import ServerlessInferenceConfig

    logger.info("Desplegando endpoint serverless '%s'", endpoint_name)
    predictor = model.deploy(
        serverless_inference_config=ServerlessInferenceConfig(
            memory_size_in_mb=memory_mb,
            max_concurrency=concurrency,
        ),
        endpoint_name=endpoint_name,
    )
    logger.info("Endpoint activo: %s", endpoint_name)
    logger.info("Ahora exporta: SCORER_BACKEND=sagemaker SAGEMAKER_ENDPOINT=%s", endpoint_name)
    return predictor


def smoke_test(endpoint_name: str, region: str) -> None:
    """Una invocacion real, para tener la captura de que responde."""
    import boto3

    client = boto3.client("sagemaker-runtime", region_name=region)
    # 11 features en el orden de Settings.feature_names.
    row = "9.9,1.0,1,5,120000,2,365,0.05,0,14,0"
    response = client.invoke_endpoint(
        EndpointName=endpoint_name, ContentType="text/csv", Body=row.encode()
    )
    body = response["Body"].read().decode("utf-8").strip()
    logger.info("Respuesta del endpoint: %s", body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Entrena y despliega en SageMaker")
    parser.add_argument("--role-arn", required=True, help="Rol de ejecucion de SageMaker")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--data", default="data/creditcard.csv")
    parser.add_argument("--bucket", default=None, help="Por omision, el bucket por defecto")
    parser.add_argument("--prefix", default="sentinel-ia")
    parser.add_argument("--instance", default=DEFAULT_TRAIN_INSTANCE)
    parser.add_argument("--endpoint-name", default=None)
    parser.add_argument("--skip-training", action="store_true", help="Despliega el ultimo modelo")
    parser.add_argument("--model-data", default=None, help="S3 URI con --skip-training")
    args = parser.parse_args()

    import sagemaker

    session = sagemaker.Session()
    bucket = args.bucket or session.default_bucket()
    endpoint_name = args.endpoint_name or f"sentinel-ia-{datetime.now(UTC):%Y%m%d-%H%M}"

    if args.skip_training:
        if not args.model_data:
            raise SystemExit("--skip-training necesita --model-data con el S3 URI del modelo")
        from sagemaker.xgboost.model import XGBoostModel

        model = XGBoostModel(
            model_data=args.model_data,
            role=args.role_arn,
            entry_point="inference.py",
            source_dir="src/training",
            framework_version="1.7-1",
        )
    else:
        train_s3 = upload_dataset(session, bucket, args.prefix, args.data)
        estimator = train(args.role_arn, args.region, train_s3, args.instance)
        model = estimator.create_model()

    deploy_serverless(model, endpoint_name)
    smoke_test(endpoint_name, args.region)

    logger.info("")
    logger.info("Recuerda capturar para el informe:")
    logger.info("  - la pantalla del endpoint en la consola de SageMaker")
    logger.info("  - el log del training job en CloudWatch")
    logger.info("  - las metricas Invocations y ModelLatency del endpoint")
    logger.info("Y borrar el endpoint cuando termines, si no quieres gastar credito.")


if __name__ == "__main__":
    main()
