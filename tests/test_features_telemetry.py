"""Casos CP-11 y CP-12: features y metricas.

Trazabilidad: EP2 seccion 5.2 (FeatureAssembler, Telemetry), restriccion RT2
(feature parity) y pauta EP3 punto 2.3 (latencia p50, p95, p99).
"""

from __future__ import annotations

from src.decision_api.config import Settings
from src.decision_api.features import FeatureAssembler
from src.decision_api.schemas import Decision
from src.decision_api.telemetry import Telemetry

from .conftest import make_tx


class TestCP11FeatureParity:
    """CP-11: el vector sale en el orden que espera el modelo, siempre."""

    def test_el_vector_respeta_el_orden_declarado(self) -> None:
        names = Settings().feature_names
        features = FeatureAssembler().build(make_tx())
        vector = features.as_vector(names)

        assert len(vector) == len(names)
        # Un orden distinto hace que el modelo lea el monto como si fuera la
        # hora: es el error silencioso que la restriccion RT2 busca evitar.
        assert vector[names.index("hour_of_day")] == 14.0
        assert vector[names.index("is_foreign_country")] == 0.0

    def test_sin_feature_store_la_decision_queda_degradada(self) -> None:
        features = FeatureAssembler(store=None).build(make_tx())
        assert features.degraded is True
        assert "feature_store" in features.missing

    def test_pais_extranjero_marca_la_feature(self) -> None:
        features = FeatureAssembler().build(make_tx(country="AR"))
        assert features.values["is_foreign_country"] == 1.0

    def test_dispositivo_desconocido_marca_la_feature(self) -> None:
        features = FeatureAssembler().build(make_tx(device_token="dev_nuevo"))
        assert features.values["is_new_device"] == 1.0

    def test_una_feature_faltante_se_rellena_con_cero(self) -> None:
        # Un modelo nuevo puede pedir una feature que el store aun no tiene.
        features = FeatureAssembler().build(make_tx())
        vector = features.as_vector(["amount_log", "feature_que_no_existe"])
        assert vector[1] == 0.0


class TestCP12Percentiles:
    """CP-12: p50, p95 y p99 se calculan bien."""

    def test_percentiles_sobre_una_muestra_conocida(self) -> None:
        telemetry = Telemetry()
        # 1 a 100 ms: con 100 muestras los percentiles son predecibles.
        for value in range(1, 101):
            telemetry.observe(Decision.APPROVE, float(value), degraded=False)

        q = telemetry.quantiles()
        assert q["p50"] == 50.5
        assert 95.0 <= q["p95"] <= 96.5
        assert 99.0 <= q["p99"] <= 100.0

    def test_sin_muestras_los_percentiles_son_cero(self) -> None:
        assert Telemetry().quantiles() == {"p50": 0.0, "p95": 0.0, "p99": 0.0}

    def test_una_sola_muestra(self) -> None:
        telemetry = Telemetry()
        telemetry.observe(Decision.REJECT, 42.0, degraded=False)
        q = telemetry.quantiles()
        assert q["p50"] == q["p95"] == q["p99"] == 42.0

    def test_el_snapshot_cuenta_decisiones_y_degradadas(self) -> None:
        telemetry = Telemetry()
        telemetry.observe(Decision.APPROVE, 10.0, degraded=False)
        telemetry.observe(Decision.REJECT, 20.0, degraded=True)
        telemetry.observe(Decision.REVIEW, 30.0, degraded=False)

        snapshot = telemetry.snapshot()
        assert snapshot["requests_total"] == 3
        assert snapshot["decisions"]["APPROVE"] == 1
        assert snapshot["counters"]["degraded"] == 1
        assert snapshot["latency_mean_ms"] == 20.0

    def test_el_formato_prometheus_trae_las_series_esperadas(self) -> None:
        telemetry = Telemetry()
        telemetry.observe(Decision.APPROVE, 12.0, degraded=False)
        telemetry.rate_limited()
        text = telemetry.render()

        assert "sentinel_requests_total 1" in text
        assert 'sentinel_decisions_total{decision="APPROVE"} 1' in text
        assert "sentinel_rate_limited_total 1" in text
        # El histograma necesita el bucket +Inf para ser valido.
        assert 'sentinel_latency_ms_bucket{le="+Inf"} 1' in text
        assert "sentinel_latency_ms_count 1" in text

    def test_el_histograma_es_acumulativo(self) -> None:
        telemetry = Telemetry()
        telemetry.observe(Decision.APPROVE, 15.0, degraded=False)
        text = telemetry.render()
        # 15 ms cuenta en el bucket de 20 y en todos los mayores, no en el de 10.
        assert 'sentinel_latency_ms_bucket{le="10"} 0' in text
        assert 'sentinel_latency_ms_bucket{le="20"} 1' in text
        assert 'sentinel_latency_ms_bucket{le="100"} 1' in text
