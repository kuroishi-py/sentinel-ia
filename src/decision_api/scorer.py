"""ModelScorer y ModelLoader (EP2 secciones 5.2 y 5.3).

Hay dos backends, y la diferencia entre ellos es justamente el punto que la
EP2 decidio (D2) y la pauta de la EP3 exige:

- ``LocalScorer``     : el modelo vive en memoria dentro de la API. Es lo que
                        la EP2 eligio para cumplir el p99 < 100 ms.
- ``SageMakerScorer`` : el modelo vive en un endpoint de SageMaker, que es lo
                        que la pauta de la EP3 pide desplegar.

Los dos implementan la misma interfaz, asi que se puede medir el p99 de ambos
con la misma prueba de carga y reportar la diferencia en el informe. Eso
convierte el conflicto en un resultado medido.

El cargador verifica el SHA-256 del artefacto contra el valor esperado antes de
crear el scorer: sin eso, sustituir el modelo es un vector de ataque de cadena
de suministro (EP2 seccion 8.2, AML.T0010).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class ModelHashMismatch(RuntimeError):
    """El artefacto en disco no es el que el registro aprobo."""


class Scorer(Protocol):
    """Lo que la API necesita de un modelo, sea local o remoto."""

    @property
    def model_version(self) -> str: ...

    def score(self, features: list[float]) -> float: ...


class ConstantScorer:
    """Scorer de respaldo. No es un modelo: es lo que queda cuando no hay uno.

    Devuelve un valor fijo en la zona de revision, de modo que sin modelo el
    sistema manda todo a revision humana en vez de aprobar a ciegas.
    """

    def __init__(self, value: float = 0.5, version: str = "none") -> None:
        self._value = value
        self._version = version

    @property
    def model_version(self) -> str:
        return self._version

    def score(self, features: list[float]) -> float:
        return self._value


class LocalScorer:
    """XGBoost cargado en memoria. Un salto de red menos (EP2 D2)."""

    def __init__(self, booster, model_version: str, feature_names: list[str]) -> None:
        self._booster = booster
        self._version = model_version
        self._feature_names = feature_names

    @property
    def model_version(self) -> str:
        return self._version

    def score(self, features: list[float]) -> float:
        import numpy as np  # local: solo se necesita con este backend

        matrix = np.asarray([features], dtype=np.float32)
        raw = self._booster.inplace_predict(matrix)
        return float(raw[0])


class SageMakerScorer:
    """Endpoint de SageMaker. Es el backend que exige la pauta de la EP3."""

    def __init__(self, endpoint_name: str, model_version: str, region: str, client=None) -> None:
        self._endpoint = endpoint_name
        self._version = model_version
        if client is not None:
            self._client = client
        else:
            import boto3  # local: solo se necesita con este backend

            self._client = boto3.client("sagemaker-runtime", region_name=region)

    @property
    def model_version(self) -> str:
        return self._version

    def score(self, features: list[float]) -> float:
        payload = ",".join(str(value) for value in features)
        response = self._client.invoke_endpoint(
            EndpointName=self._endpoint,
            ContentType="text/csv",
            Body=payload.encode("utf-8"),
        )
        body = response["Body"].read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return float(body.strip().splitlines()[0].split(",")[0])


def sha256_of(path: str | Path) -> str:
    """SHA-256 de un archivo, leido por bloques."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelLoader:
    """Decide que scorer usar y lo deja listo antes de marcar /health/ready."""

    def __init__(self, settings) -> None:
        self._settings = settings

    def load(self) -> Scorer:
        backend = self._settings.scorer_backend.lower()
        if backend == "sagemaker":
            return self._load_sagemaker()
        if backend == "local":
            return self._load_local()
        logger.warning("Backend '%s' desconocido; se usa el scorer de respaldo", backend)
        return ConstantScorer(version=self._settings.model_version)

    def _load_sagemaker(self) -> Scorer:
        endpoint = self._settings.sagemaker_endpoint
        if not endpoint:
            logger.error("SCORER_BACKEND=sagemaker pero SAGEMAKER_ENDPOINT esta vacio")
            return ConstantScorer(version=self._settings.model_version)
        logger.info("Scorer: endpoint de SageMaker '%s'", endpoint)
        return SageMakerScorer(
            endpoint_name=endpoint,
            model_version=self._settings.model_version,
            region=self._settings.aws_region,
        )

    def _load_local(self) -> Scorer:
        path = Path(self._settings.model_path)
        if not path.exists():
            logger.warning(
                "No hay artefacto en %s; todo ira a revision hasta que se entrene un modelo "
                "(python -m src.training.train)",
                path,
            )
            return ConstantScorer(version=self._settings.model_version)

        self._verify_hash(path)

        try:
            import xgboost as xgb
        except ImportError:
            logger.error("xgboost no esta instalado; se usa el scorer de respaldo")
            return ConstantScorer(version=self._settings.model_version)

        booster = xgb.Booster()
        booster.load_model(str(path))
        # Un hilo por proceso. Por omision XGBoost abre un hilo OpenMP por
        # nucleo para cada prediccion; con gunicorn en varios workers eso
        # sobresuscribe la CPU, y la espera activa de OpenMP la consume. Para
        # predecir una fila no hay nada que paralelizar: la concurrencia la dan
        # los workers. La prueba de carga del 26-11-2026 lo mostro en Docker
        # con 2 vCPU: p99 interno de 62 ms y /health/ready tardando segundos.
        booster.set_param({"nthread": 1})
        version = self._read_version(path) or self._settings.model_version

        expected = len(self._settings.feature_names)
        trained_on = self._trained_features(path)
        if trained_on is not None and len(trained_on) != expected:
            # Pasa al entrenar con un dataset publico cuyas columnas no son las
            # del diseno: el de la ULB trae 30 componentes principales, no las
            # 11 features con nombre que FeatureAssembler sabe construir.
            # Mejor decirlo aqui que dejar que XGBoost falle en cada solicitud.
            logger.error(
                "El modelo %s espera %d features (%s...) y FeatureAssembler produce %d. "
                "No se puede servir por /v1/score: o se entrena con las features del "
                "diseno, o la pasarela envia el vector ya calculado.",
                version,
                len(trained_on),
                ", ".join(trained_on[:4]),
                expected,
            )
            return ConstantScorer(version=version)

        scorer = LocalScorer(booster, version, self._settings.feature_names)
        # Warm-up: la primera prediccion de XGBoost es mas lenta que las
        # siguientes, y no queremos que ese costo lo pague una transaccion real.
        scorer.score([0.0] * expected)
        logger.info("Scorer: modelo local %s (%s)", version, path)
        return scorer

    @staticmethod
    def _trained_features(path: Path) -> list[str] | None:
        """Features con las que se entrenó el artefacto, según su metadata."""
        meta = path.with_name("metadata.json")
        if not meta.exists():
            return None
        try:
            features = json.loads(meta.read_text(encoding="utf-8")).get("features")
        except (json.JSONDecodeError, OSError):
            return None
        return features if isinstance(features, list) else None

    def _verify_hash(self, path: Path) -> None:
        """Compara el artefacto con el hash que dejo el entrenamiento."""
        sidecar = path.with_suffix(path.suffix + ".sha256")
        if not sidecar.exists():
            logger.warning("Sin %s: no se puede verificar el artefacto", sidecar.name)
            return
        expected = sidecar.read_text(encoding="utf-8").split()[0].strip()
        actual = sha256_of(path)
        if expected != actual:
            raise ModelHashMismatch(
                f"El modelo en {path} no coincide con el hash aprobado "
                f"(esperado {expected[:12]}..., leido {actual[:12]}...)"
            )

    @staticmethod
    def _read_version(path: Path) -> str | None:
        meta = path.with_name("metadata.json")
        if not meta.exists():
            return None
        try:
            return json.loads(meta.read_text(encoding="utf-8")).get("model_version")
        except (json.JSONDecodeError, OSError):
            return None
