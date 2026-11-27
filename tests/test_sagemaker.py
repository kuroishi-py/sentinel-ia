"""Caso CP-25: backend y despliegue en SageMaker.

Trazabilidad: pauta EP3 punto 1.2 (SageMaker: endpoints) y decision D2 de la
EP2. Sin cuenta de AWS se prueba todo lo que no es la llamada de red: el
contrato con el endpoint, que una falla no tumbe el pago, que la llamada no
bloquee el proceso, el empaquetado del artefacto verificado y que el archivo de
paridad corresponda al modelo versionado.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("xgboost")

from fastapi.testclient import TestClient  # noqa: E402

from src.decision_api.config import Settings  # noqa: E402
from src.decision_api.scorer import ModelLoader, SageMakerScorer  # noqa: E402
from src.training import inference, sagemaker_deploy  # noqa: E402

from .test_api import API_KEY, configure_env, payload  # noqa: E402

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"


class FakeRuntime:
    """Imita boto3 sagemaker-runtime: guarda lo que recibe y responde un CSV."""

    def __init__(self, answer: str = "0.912345\n", error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._answer = answer
        self._error = error

    def invoke_endpoint(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"Body": io.BytesIO(self._answer.encode("utf-8"))}


@pytest.fixture(scope="module")
def app_client():
    configure_env()
    from src.decision_api.config import reset_settings

    reset_settings()
    import importlib

    from src.decision_api import main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as test_client:
        yield test_client, main_module
    reset_settings()


class TestCP25SageMaker:
    """CP-25: el endpoint sirve el mismo modelo que la API, sin poner en riesgo el pago."""

    def test_envia_las_11_features_en_csv_y_lee_la_probabilidad(self) -> None:
        runtime = FakeRuntime()
        scorer = SageMakerScorer("sentinel-ia-test", "v-sm", "us-east-1", client=runtime)
        score = scorer.score([0.5] * len(Settings().feature_names))

        assert score == pytest.approx(0.912345)
        call = runtime.calls[0]
        assert call["EndpointName"] == "sentinel-ia-test"
        assert call["ContentType"] == "text/csv"
        assert len(call["Body"].decode().split(",")) == 11

    def test_la_llamada_se_ejecuta_fuera_del_event_loop(self) -> None:
        # Si corriera dentro del event loop, cada invocacion (decenas de ms)
        # detendria todas las demas solicitudes del proceso.
        assert SageMakerScorer.blocking_io is True

    def test_endpoint_caido_degrada_y_no_aprueba(self, app_client) -> None:
        client, main_module = app_client
        state = main_module.state
        original = state.scorer
        state.scorer = SageMakerScorer(
            "sentinel-ia-test", "v-sm", "us-east-1", client=FakeRuntime(error=TimeoutError())
        )
        try:
            response = client.post(
                "/v1/score", json=payload(id_tx="tx-cp25-caido"), headers={"X-API-Key": API_KEY}
            )
        finally:
            state.scorer = original
        assert response.status_code == 200
        body = response.json()
        assert body["degraded"] is True
        assert body["decision"] != "APPROVE"

    def test_el_handler_del_endpoint_usa_el_contrato_de_la_api(self) -> None:
        assert Settings().feature_names == inference.FEATURE_NAMES

    def test_la_paridad_corresponde_al_modelo_versionado(self) -> None:
        # Si alguien reentrena sin regenerar el archivo, esta prueba avisa:
        # el despliegue compararia contra scores de otro modelo.
        parity = json.loads((ARTIFACTS / "sagemaker_parity.json").read_text(encoding="utf-8"))
        settings = Settings(model_path=str(ARTIFACTS / "model.json"), scorer_backend="local")
        scorer = ModelLoader(settings).load()

        assert parity["feature_names"] == settings.feature_names
        assert parity["model_version"] == scorer.model_version
        for case in parity["casos"]:
            assert scorer.score(case["vector"]) == pytest.approx(case["score_api"], abs=1e-6)
        scores = {c["escenario"]: c["score_api"] for c in parity["casos"]}
        assert scores["compra_normal"] < 0.45 <= 0.80 <= scores["toma_de_cuenta"]

    def test_se_empaqueta_solo_el_artefacto_verificado(self, tmp_path, monkeypatch) -> None:
        model = sagemaker_deploy.verified_model()
        tarball = sagemaker_deploy.package(model, tmp_path)
        with tarfile.open(tarball) as tar:
            assert tar.getnames() == ["model.json"]

        # Un artefacto alterado no se despliega (EP2 8.2, AML.T0010).
        altered = tmp_path / "artifacts"
        altered.mkdir()
        (altered / "model.json").write_bytes(model.read_bytes() + b" ")
        (altered / "model.json.sha256").write_text(
            (ARTIFACTS / "model.json.sha256").read_text(encoding="utf-8")
        )
        monkeypatch.setattr(sagemaker_deploy, "ARTIFACTS", altered)
        with pytest.raises(SystemExit, match="no coincide"):
            sagemaker_deploy.verified_model()
