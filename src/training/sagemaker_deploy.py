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

Uso (desde la terminal de AWS Academy o CloudShell; solo necesita boto3):
    python src/training/sagemaker_deploy.py --region us-east-1
    python src/training/sagemaker_deploy.py --region us-east-1 --instance-type ml.m5.large
    python src/training/sagemaker_deploy.py --region us-east-1 --check sentinel-ia-20261127-1600
    python src/training/sagemaker_deploy.py --region us-east-1 --delete sentinel-ia-20261127-1600

Sin --role-arn se usa el rol LabRole de la cuenta (AWS Academy Learner Lab).

Compatible con Python 3.7 (la terminal de AWS Academy): no importa nada de src/
ni el SDK de SageMaker.
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
#: Cuentas de ECR de la imagen XGBoost administrada (sagemaker.image_uris).
XGBOOST_IMAGE_ACCOUNTS = {"us-east-1": "683313688378", "us-west-2": "246618743249"}
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


def package_code(workdir: Path) -> Path:
    """sourcedir.tar.gz con el handler, como lo arma el SDK de SageMaker."""
    tarball = workdir / "sourcedir.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(SOURCE_DIR / "inference.py", arcname="inference.py")
    return tarball


def image_uri(region: str, override: str | None = None) -> str:
    if override:
        return override
    account = XGBOOST_IMAGE_ACCOUNTS.get(region)
    if account is None:
        raise SystemExit(f"Sin imagen XGBoost conocida para {region}: pasa --image-uri")
    return f"{account}.dkr.ecr.{region}.amazonaws.com/sagemaker-xgboost:{FRAMEWORK_VERSION}"


def container_env(code_uri: str, region: str) -> dict:
    """Las variables que fija el SDK para un XGBoostModel con entry_point.

    El contenedor descarga sourcedir.tar.gz desde S3 y usa inference.py como
    handler (model_fn, input_fn, predict_fn, output_fn).
    """
    return {
        "SAGEMAKER_PROGRAM": "inference.py",
        "SAGEMAKER_SUBMIT_DIRECTORY": code_uri,
        "SAGEMAKER_CONTAINER_LOG_LEVEL": "20",
        "SAGEMAKER_REGION": region,
    }


def production_variant(model_name: str, args) -> dict:
    variant = {"VariantName": "AllTraffic", "ModelName": model_name}
    if args.instance_type:
        # Endpoint de instancia: sin arranque en frio, pero cobra por hora.
        variant.update(InitialInstanceCount=1, InstanceType=args.instance_type, InitialVariantWeight=1.0)
    else:
        # Serverless: escala con las invocaciones hasta MaxConcurrency y baja a
        # cero sin trafico. 2048 MB va alineado con la tarea de 2 GB de la EP2.
        variant["ServerlessConfig"] = {"MemorySizeInMB": args.memory, "MaxConcurrency": args.concurrency}
    return variant


def put_file(s3, path: Path, bucket: str, key: str) -> None:
    """Sube un archivo chico con una sola solicitud.

    No se usa ``upload_file``: reparte la subida en hilos, y el Python 3.7 de la
    terminal de AWS Academy se cae con "Segmentation fault" al cerrarlos
    (27-11-2026). Los artefactos pesan pocos KB, asi que no hace falta.
    """
    s3.put_object(Bucket=bucket, Key=key, Body=path.read_bytes())


def ensure_bucket(s3, bucket: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:  # noqa: BLE001 - no existe o no es nuestro: se intenta crear
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})


def deploy(args, session=None) -> str:
    """Crea modelo, configuracion y endpoint solo con boto3.

    No usa el SDK de SageMaker a proposito: la terminal de AWS Academy trae
    Python 3.7 y el SDK no se instala bien ahi. Se replica lo que hace el SDK
    para un XGBoostModel (codigo en S3 y variables SAGEMAKER_*).
    """
    if session is None:
        import boto3

        session = boto3.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    role = args.role_arn or f"arn:aws:iam::{account}:role/LabRole"
    bucket = args.bucket or f"sagemaker-{args.region}-{account}"
    # timezone.utc y no datetime.UTC: la terminal de AWS Academy trae Python 3.7.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")  # noqa: UP017
    name = args.endpoint_name or f"sentinel-ia-{stamp}"

    model = verified_model()
    version = model_version()
    s3 = session.client("s3")
    ensure_bucket(s3, bucket, args.region)
    prefix = f"{args.prefix}/{version}/{stamp}"
    with tempfile.TemporaryDirectory() as tmp:
        put_file(s3, package(model, Path(tmp)), bucket, f"{prefix}/model.tar.gz")
        put_file(s3, package_code(Path(tmp)), bucket, f"{prefix}/sourcedir.tar.gz")
    model_uri = f"s3://{bucket}/{prefix}/model.tar.gz"
    code_uri = f"s3://{bucket}/{prefix}/sourcedir.tar.gz"
    logger.info("Modelo %s y handler subidos a s3://%s/%s/", version, bucket, prefix)

    sm = session.client("sagemaker")
    sm.create_model(
        ModelName=name,
        ExecutionRoleArn=role,
        PrimaryContainer={
            "Image": image_uri(args.region, args.image_uri),
            "ModelDataUrl": model_uri,
            "Environment": container_env(code_uri, args.region),
        },
        Tags=[{"Key": "model_version", "Value": version}],
    )
    sm.create_endpoint_config(EndpointConfigName=name, ProductionVariants=[production_variant(name, args)])
    sm.create_endpoint(EndpointName=name, EndpointConfigName=name)
    kind = args.instance_type or f"serverless {args.memory} MB, concurrencia {args.concurrency}"
    logger.info("Creando endpoint '%s' (%s). Toma de 3 a 10 minutos...", name, kind)

    try:
        sm.get_waiter("endpoint_in_service").wait(
            EndpointName=name, WaiterConfig={"Delay": 20, "MaxAttempts": 45}
        )
    except Exception as exc:  # noqa: BLE001 - se informa el motivo que da SageMaker
        reason = sm.describe_endpoint(EndpointName=name).get("FailureReason", str(exc))
        raise SystemExit(f"El endpoint {name} no quedo en servicio: {reason}") from exc
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
    parser.add_argument("--image-uri", default=None, help="Imagen XGBoost, si la region no esta en la tabla")
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
