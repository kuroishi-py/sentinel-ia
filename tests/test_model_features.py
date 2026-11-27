"""Casos CP-20 a CP-22: features de velocidad, modelo servido y explicacion.

Trazabilidad: restriccion RT2 (feature parity), EP2 seccion 8.2 (restricciones
monotonicas contra evasion, SHA-256 del modelo), RF2 y RF4, y pauta EP3 punto
2.1 (interpretabilidad para la toma de decisiones).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.decision_api.config import Settings
from src.decision_api.features import (
    FeatureAssembler,
    MemoryFeatureState,
    MerchantRisk,
    RedisFeatureState,
)

from .conftest import make_tx

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"
MODEL = ARTIFACTS / "model.json"
T0 = datetime(2026, 11, 22, 14, 0, tzinfo=UTC)


def replay(assembler: FeatureAssembler, txs):
    """Pasa una secuencia de transacciones y devuelve las features de cada una."""
    return [assembler.build(tx).values for tx in txs]


class TestCP20VentanasDeVelocidad:
    """CP-20: los contadores ven solo el pasado y respetan sus ventanas."""

    def test_la_transaccion_actual_no_se_cuenta_a_si_misma(self) -> None:
        values = FeatureAssembler(MemoryFeatureState()).build(make_tx(timestamp=T0)).values
        assert values["tx_count_1h"] == 0.0
        assert values["tx_count_24h"] == 0.0

    def test_ventana_de_una_hora_y_de_24_horas(self) -> None:
        txs = [
            make_tx(id_tx="tx-cp20-a", timestamp=T0, merchant_id="mrc_1"),
            make_tx(id_tx="tx-cp20-b", timestamp=T0 + timedelta(hours=2), merchant_id="mrc_2"),
            make_tx(id_tx="tx-cp20-c", timestamp=T0 + timedelta(hours=2, minutes=30), merchant_id="mrc_2"),
        ]
        last = replay(FeatureAssembler(MemoryFeatureState()), txs)[-1]
        assert last["tx_count_1h"] == 1.0  # solo b, a quedo fuera de la hora
        assert last["tx_count_24h"] == 2.0
        assert last["distinct_merchants_24h"] == 2.0
        assert last["amount_sum_24h"] == pytest.approx(2 * 19_990.0)

    def test_lo_de_hace_mas_de_24_horas_ya_no_cuenta(self) -> None:
        txs = [
            make_tx(id_tx="tx-cp20-d", timestamp=T0),
            make_tx(id_tx="tx-cp20-e", timestamp=T0 + timedelta(hours=25)),
        ]
        last = replay(FeatureAssembler(MemoryFeatureState()), txs)[-1]
        assert last["tx_count_24h"] == 0.0
        assert last["user_age_days"] == pytest.approx(25 / 24, rel=1e-3)

    def test_monto_sobre_el_promedio_del_usuario(self) -> None:
        txs = [make_tx(id_tx=f"tx-cp20-p{i}", amount=10_000.0, timestamp=T0 + timedelta(days=i)) for i in range(3)]
        txs.append(make_tx(id_tx="tx-cp20-grande", amount=90_000.0, timestamp=T0 + timedelta(days=3)))
        last = replay(FeatureAssembler(MemoryFeatureState()), txs)[-1]
        assert last["amount_vs_user_avg"] == pytest.approx(9.0)

    def test_un_dispositivo_es_nuevo_durante_sus_primeras_24_horas(self) -> None:
        assembler = FeatureAssembler(MemoryFeatureState())
        first = assembler.build(make_tx(id_tx="tx-cp20-d1", device_token="dev_x", timestamp=T0)).values
        same_day = assembler.build(
            make_tx(id_tx="tx-cp20-d2", device_token="dev_x", timestamp=T0 + timedelta(hours=3))
        ).values
        next_week = assembler.build(
            make_tx(id_tx="tx-cp20-d3", device_token="dev_x", timestamp=T0 + timedelta(days=7))
        ).values
        assert first["is_new_device"] == 1.0
        assert same_day["is_new_device"] == 1.0
        assert next_week["is_new_device"] == 0.0

    def test_el_riesgo_del_comercio_sale_de_la_tabla_entrenada(self) -> None:
        risk = MerchantRisk({"mrc_riesgoso": 0.2}, prior=0.005)
        assembler = FeatureAssembler(MemoryFeatureState(), merchant_risk=risk)
        assert assembler.build(make_tx(id_tx="tx-cp20-m1", merchant_id="mrc_riesgoso")).values["merchant_risk"] == 0.2
        assert assembler.build(make_tx(id_tx="tx-cp20-m2", merchant_id="mrc_nuevo")).values["merchant_risk"] == 0.005

    def test_redis_y_memoria_calculan_lo_mismo(self) -> None:
        """Feature parity entre los dos backends, con un Redis de verdad."""
        redis = pytest.importorskip("redis")
        client = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/15"), socket_timeout=0.5)
        try:
            client.ping()
        except Exception:  # noqa: BLE001
            pytest.skip("sin Redis en REDIS_URL")
        user = f"usr_parity_{os.getpid()}"
        client.delete(f"vel:{user}", f"profile:{user}", f"devices:{user}")
        txs = [
            make_tx(
                id_tx=f"tx-cp20-r{i}",
                user_token=user,
                merchant_id=f"mrc_{i % 3}",
                amount=1_000.0 * (i + 1),
                device_token="dev_a" if i < 4 else "dev_b",
                timestamp=T0 + timedelta(minutes=20 * i),
            )
            for i in range(8)
        ]
        try:
            from_redis = replay(FeatureAssembler(RedisFeatureState(client)), txs)
        finally:
            client.delete(f"vel:{user}", f"profile:{user}", f"devices:{user}")
        from_memory = replay(FeatureAssembler(MemoryFeatureState()), txs)
        for r, m in zip(from_redis, from_memory, strict=True):
            assert r == pytest.approx(m)


class TestCP20bParidadEntrenamientoServicio:
    """CP-20b: el entrenamiento ve exactamente las features que vera la API (RT2)."""

    @pytest.fixture(scope="class")
    def small(self):
        pytest.importorskip("pandas")
        from src.training.simulate import SimulationConfig, simulate

        config = SimulationConfig(
            n_users=80,
            n_merchants=40,
            days=12,
            reference_days=4,
            test_days=3,
            ato_events=6,
            card_testing_events=3,
            fraud_accounts=3,
        )
        return simulate(config)

    def test_la_simulacion_trae_fraude_y_particion_temporal(self, small) -> None:
        frame, _, _ = small
        assert set(frame["period"]) == {"reference", "train", "test"}
        assert frame["Class"].sum() > 0
        assert frame["ts"].is_monotonic_increasing
        # La particion es temporal: todo el test es posterior a todo el train.
        assert frame[frame["period"] == "test"]["ts"].min() > frame[frame["period"] == "train"]["ts"].max()

    def test_el_riesgo_por_comercio_sale_solo_del_periodo_de_referencia(self, small) -> None:
        frame, _, meta = small
        reference = set(frame[frame["period"] == "reference"]["merchant_id"])
        assert set(meta["table"]) == reference

    def test_reproducir_por_la_api_da_las_mismas_features(self, small) -> None:
        from src.decision_api.schemas import ScoreRequest

        frame, risk, _ = small
        assembler = FeatureAssembler(MemoryFeatureState(), merchant_risk=risk)
        names = Settings().feature_names
        for rec in frame.to_dict("records"):
            # model_validate: la misma validacion que hace FastAPI con el JSON.
            request = ScoreRequest.model_validate(
                {
                    "id_tx": rec["id_tx"],
                    "user_token": rec["user_token"],
                    "merchant_id": rec["merchant_id"],
                    "amount": rec["amount"],
                    "currency": "CLP",
                    "country": rec["country"],
                    "device_token": rec["device_token"],
                    "mcc": rec["mcc"],
                    "timestamp": rec["ts"].isoformat(),
                }
            )
            served = assembler.build(request).values
            assert [served[n] for n in names] == pytest.approx([rec[n] for n in names]), rec["id_tx"]


@pytest.fixture(scope="module")
def served():
    if not MODEL.exists():
        pytest.skip("sin artifacts/model.json: corre python -m src.training.train --simulated")
    from src.decision_api.scorer import ModelLoader

    settings = Settings(model_path=str(MODEL), scorer_backend="local")
    return settings, ModelLoader(settings).load()


def vector(settings: Settings, **overrides: float) -> list[float]:
    """Una transaccion corriente: usuario de un ano, de dia, en Chile."""
    base = {
        "amount_log": 9.9,
        "amount_vs_user_avg": 1.0,
        "tx_count_1h": 0.0,
        "tx_count_24h": 1.0,
        "amount_sum_24h": 20_000.0,
        "distinct_merchants_24h": 1.0,
        "user_age_days": 365.0,
        "merchant_risk": 0.004,
        "is_foreign_country": 0.0,
        "hour_of_day": 17.0,
        "is_new_device": 0.0,
    }
    base.update(overrides)
    return [base[name] for name in settings.feature_names]


class TestCP21ModeloServido:
    """CP-21: el artefacto que sirve la API es el aprobado y se comporta como tal."""

    def test_el_contrato_de_features_coincide_con_la_api(self) -> None:
        metadata = json.loads((ARTIFACTS / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["features"] == Settings().feature_names

    def test_carga_el_modelo_real_y_no_el_de_respaldo(self, served) -> None:
        from src.decision_api.scorer import LocalScorer

        _, scorer = served
        assert isinstance(scorer, LocalScorer)
        assert scorer.model_version != "none"

    def test_una_transaccion_corriente_puntua_bajo(self, served) -> None:
        settings, scorer = served
        assert scorer.score(vector(settings)) < settings.threshold_review

    def test_una_toma_de_cuenta_puntua_alto(self, served) -> None:
        settings, scorer = served
        ato = vector(
            settings,
            amount_log=12.3,
            amount_vs_user_avg=9.0,
            tx_count_1h=3.0,
            tx_count_24h=4.0,
            amount_sum_24h=600_000.0,
            merchant_risk=0.05,
            is_foreign_country=1.0,
            hour_of_day=6.0,
            is_new_device=1.0,
        )
        assert scorer.score(ato) >= settings.threshold_reject

    @pytest.mark.parametrize(
        ("feature", "low", "high"),
        [
            ("tx_count_1h", 0.0, 6.0),
            ("amount_vs_user_avg", 1.0, 10.0),
            ("is_new_device", 0.0, 1.0),
            ("is_foreign_country", 0.0, 1.0),
            ("merchant_risk", 0.0, 0.2),
        ],
    )
    def test_mas_sospechoso_nunca_puntua_menos(self, served, feature, low, high) -> None:
        # Restriccion monotonica (EP2 8.2, AML.T0015): quien sondea la API no
        # encuentra una zona donde subir una senal de riesgo baje el score.
        settings, scorer = served
        assert scorer.score(vector(settings, **{feature: high})) >= scorer.score(vector(settings, **{feature: low}))

    def test_un_artefacto_alterado_no_se_carga(self, tmp_path) -> None:
        from src.decision_api.scorer import ModelHashMismatch, ModelLoader

        if not MODEL.exists():
            pytest.skip("sin artefacto")
        tampered = tmp_path / "model.json"
        tampered.write_bytes(MODEL.read_bytes() + b" ")
        (tmp_path / "model.json.sha256").write_text((ARTIFACTS / "model.json.sha256").read_text(), encoding="utf-8")
        with pytest.raises(ModelHashMismatch):
            ModelLoader(Settings(model_path=str(tampered))).load()


class TestCP22Explicacion:
    """CP-22: cada rechazo trae una explicacion que un analista puede leer."""

    @pytest.fixture(scope="class")
    def explainer(self, served):
        pytest.importorskip("shap")
        from src.shap_worker.main import ShapExplainer

        settings, _ = served
        return settings, ShapExplainer(settings)

    def test_la_explicacion_suma_el_score(self, explainer, served) -> None:
        import math

        settings, shap_explainer = explainer
        _, scorer = served
        features = dict(
            zip(settings.feature_names, vector(settings, is_new_device=1.0, tx_count_1h=4.0), strict=True)
        )
        explanation = shap_explainer.explain("tx-cp22", features)
        assert explanation is not None
        assert set(explanation.contributions) == set(settings.feature_names)
        # TreeSHAP es exacto: base + aportes = logit del score.
        logit = explanation.base_value + sum(explanation.contributions.values())
        prob = scorer.score([features[n] for n in settings.feature_names])
        assert 1 / (1 + math.exp(-logit)) == pytest.approx(prob, abs=1e-4)

    def test_las_razones_principales_son_las_senales_de_la_toma_de_cuenta(self, explainer) -> None:
        settings, shap_explainer = explainer
        features = dict(
            zip(
                settings.feature_names,
                vector(settings, is_new_device=1.0, tx_count_1h=4.0, is_foreign_country=1.0, amount_vs_user_avg=8.0),
                strict=True,
            )
        )
        explanation = shap_explainer.explain("tx-cp22-ato", features)
        assert explanation is not None
        top = set(explanation.top_features)
        assert top & {"is_new_device", "tx_count_1h", "is_foreign_country", "amount_vs_user_avg"}
        assert all(explanation.contributions[name] > 0 for name in top)
