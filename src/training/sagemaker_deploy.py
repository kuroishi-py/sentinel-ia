"""Despliegue del modelo de servicio en un endpoint de SageMaker.

Este archivo resuelve el choque entre la decision D2 de la EP2 (el modelo vive
dentro de la API para ahorrar un salto de red) y la pauta de la EP3, que exige
el modelo "desplegado en AWS SageMaker" con "endpoints configurados".

No se reentrena en AWS: se publica **el mismo artefacto versionado** que sirve
la API (``artifacts/model.json``, verificado por SHA-256). Asi los dos backends
de ``scorer.py`` sirven exactamente el mismo modelo y la comparacion de
latencia mide solo donde corre. Reentrenar en SageMaker no serviria: el modelo
de servicio se entrena sobre la historia simulada que calcula
``src.decision_api.features``, y el contenedor administrado de XGBoost no trae
ese paquete.

Flujo:
    1. Verifica el SHA-256 de artifacts/model.json.
    2. Lo empaqueta en model.tar.gz y lo sube a S3.
    3. Crea el modelo con el contenedor XGBoost 1.7-1 y src/training/inference.py
       como handler (CSV de 11 features -> probabilidad).
    4. Despliega un endpoint Serverless Inference (o de instancia, con
       --instance-type).
    5. Envia las transacciones de artifacts/sagemaker_parity.json y compara el
       score del endpoint con el de la API.

Uso (por ejemplo desde AWS CloudShell, con ``pip install sagemaker``):
    python src/training/sagemaker_deploy.py --region us-east-1
    python src/training/sagemaker_deploy.py --region us-east-1 --instance-type ml.m5.large
    python src/training/sagemaker_deploy.py --region us-east-1 --check sentinel-ia-20261127-1600
    python src/training/sagemaker_deploy.py --region us-east-1 --delete sentinel-ia-20261127-1600

Sin --role-arn se usa el rol LabRole de la cuenta (AWS Academy Learner Lab).

Compatible con Python 3.9 (el de CloudShell): no importa nada de src/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sagemaker-deploy")

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts"
SOURCE_DIR = Path(__file__).resolve().parent  # src/training: trae inference.py
FRAMEWORK_VERSION = "1.7-1"
#: Diferencia tolerada entre el score de la API y el del endpoint. XGBoost 1.7
#: (endpoint) y 2.1 (API) dan el mismo resultado salvo el redondeo de float32.
TOLERANCE = 1e-4


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_model() -> Path:
    """El artefacto aprobado, solo si su SHA-256 coincide (EP2 8.2, AML.T0010)."""
    model = ARTIFACTS / "model.json"
    expected_file = ARTIFACTS / "model.json.sha256"
    if not model.exists():
        raise SystemExit(f"No existe {model}")
    actual = sha256_of(model)
    if expected_file.exists():
        expected = expected_file.read_text(encoding="utf-8").split()[0]
        if actual != expected:
            raise SystemExit(f"SHA-256 de {model} no coincide: {actual} != {expected}. No se despliega.")
        logger.info("Modelo verificado: SHA-256 %s", actual[:16])
    else:
        logger.warning("Sin %s: se despliega sin verificar", expected_file)
    return model


def model_version() -> str:
    try:
        return json.loads((ARTIFACTS / "metadata.json").read_text(encoding="utf-8"))["model_version"]
    except (OSError, ValueError, KeyError):
        return "desconocida"


def package(model: Path, workdir: Path) -> Path:
    tarball = workdir / "model.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(model, arcname="model.json")
    return tarball


def lab_role_arn(session) -> str:
    account = session.boto_session.client("sts").get_caller_identity()["Account"]
    return f"arn:aws:iam::{account}:role/LabRole"


def deploy(args) -> str:
    import boto3
    import sagemaker
    from sagemaker.xgboost.model import XGBoostModel

    session = sagemaker.Session(boto_session=boto3.Session(region_name=args.region))
    role = args.role_arn or lab_role_arn(session)
    bucket = args.bucket or session.default_bucket()
    # timezone.utc y no datetime.UTC: CloudShell trae Python 3.9.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")  # noqa: UP017
    name = args.endpoint_name or f"sentinel-ia-{stamp}"

    model = verified_model()
    version = model_version()
    with tempfile.TemporaryDirectory() as tmp:
        tarball = package(model, Path(tmp))
        model_data = session.upload_data(str(tarball), bucket=bucket, key_prefix=f"{args.prefix}/{version}")
    logger.info("Modelo %s subido a %s", version, model_data)

    sm_model = XGBoostModel(
        model_data=model_data,
        role=role,
        entry_point="inference.py",
        source_dir=str(SOURCE_DIR),
        framework_version=FRAMEWORK_VERSION,
        py_version="py3",
        sagemaker_session=session,
        name=name,
    )

    if args.instance_type:
        # Endpoint de instancia: sin arranque en frio, pero cobra por hora.
        logger.info("Desplegando endpoint '%s' en %s (toma de 5 a 10 minutos)", name, args.instance_type)
        sm_model.deploy(initial_instance_count=1, instance_type=args.instance_type, endpoint_name=name)
    else:
        # Serverless: escala con las invocaciones hasta max_concurrency y baja a
        # cero sin trafico. 2048 MB va alineado con la tarea de 2 GB de la EP2.
        from sagemaker.serverless import ServerlessInferenceConfig

        logger.info(
            "Desplegando endpoint serverless '%s' (%d MB, concurrencia %d; toma de 3 a 8 minutos)",
            name,
            args.memory,
            args.concurrency,
        )
        sm_model.deploy(
            serverless_inference_config=ServerlessInferenceConfig(
                memory_size_in_mb=args.memory, max_concurrency=args.concurrency
            ),
            endpoint_name=name,
        )
    logger.info("Endpoint activo: %s", name)
    return name


def check(name: str, region: str) -> bool:
    """Envia las transacciones de referencia y compara con el score de la API."""
    import boto3

    parity_path = ARTIFACTS / "sagemaker_parity.json"
    if not parity_path.exists():
        raise SystemExit(f"Falta {parity_path}: corre scripts/generar_paridad_sagemaker.py")
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    client = boto3.client("sagemaker-runtime", region_name=region)

    logger.info("Comparacion API (modelo en memoria) vs endpoint, modelo %s:", parity["model_version"])
    logger.info("  %-18s %12s %12s %10s %9s", "escenario", "score API", "endpoint", "diferencia", "latencia")
    ok = True
    for case in parity["casos"]:
        body = ",".join(repr(float(v)) for v in case["vector"])
        started = time.perf_counter()
        response = client.invoke_endpoint(
            EndpointName=name, ContentType="text/csv", Body=body.encode("utf-8")
        )
        elapsed = (time.perf_counter() - started) * 1000
        score = float(response["Body"].read().decode("utf-8").strip().splitlines()[0])
        diff = abs(score - case["score_api"])
        ok = ok and diff <= TOLERANCE
        logger.info(
            "  %-18s %12.6f %12.6f %10.2e %7.0f ms",
            case["escenario"],
            case["score_api"],
            score,
            diff,
            elapsed,
        )
    logger.info("Resultado: %s", "MISMO MODELO" if ok else "LOS SCORES NO COINCIDEN")
    logger.info("La primera latencia incluye el arranque en frio del endpoint serverless.")
    return ok


def delete(name: str, region: str) -> None:
    """Borra el endpoint, su configuracion y el modelo. El artefacto en S3 queda."""
    import boto3

    client = boto3.client("sagemaker", region_name=region)
    config = client.describe_endpoint(EndpointName=name)["EndpointConfigName"]
    models = [
        v["ModelName"]
        for v in client.describe_endpoint_config(EndpointConfigName=config)["ProductionVariants"]
    ]
    client.delete_endpoint(EndpointName=name)
    client.delete_endpoint_config(EndpointConfigName=config)
    for model in models:
        client.delete_model(ModelName=model)
    logger.info("Borrados el endpoint %s, su configuracion y el modelo", name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Despliega el modelo de servicio en SageMaker")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--role-arn", default=None, help="Por omision, LabRole de la cuenta")
    parser.add_argument("--bucket", default=None, help="Por omision, el bucket por defecto de SageMaker")
    parser.add_argument("--prefix", default="sentinel-ia")
    parser.add_argument("--endpoint-name", default=None)
    parser.add_argument("--memory", type=int, default=2048, help="MB del endpoint serverless (1024 a 6144)")
    parser.add_argument(
        "--concurrency", type=int, default=20, help="Concurrencia maxima del endpoint serverless"
    )
    parser.add_argument("--instance-type", default=None, help="Endpoint de instancia en vez de serverless")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", metavar="ENDPOINT", help="Solo compara el endpoint con la API")
    action.add_argument("--delete", metavar="ENDPOINT", help="Borra el endpoint y termina")
    args = parser.parse_args(argv)

    if args.delete:
        delete(args.delete, args.region)
        return 0
    if args.check:
        return 0 if check(args.check, args.region) else 1

    name = deploy(args)
    ok = check(name, args.region)
    logger.info("")
    logger.info("Para que decision-api use el endpoint, en .env:")
    logger.info("  SCORER_BACKEND=sagemaker")
    logger.info("  SAGEMAKER_ENDPOINT=%s", name)
    logger.info("  AWS_REGION=%s", args.region)
    logger.info("Capturas: el endpoint InService en la consola y sus metricas Invocations,")
    logger.info("ModelLatency y OverheadLatency en CloudWatch.")
    logger.info(
        "Al terminar: python src/training/sagemaker_deploy.py --region %s --delete %s", args.region, name
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
