"""Casos CP-05 a CP-08: motor de politicas.

Trazabilidad: EP2 seccion 5.2 (PolicyEngine), RF3, RNF6 y el escenario de
calidad "Seguridad" (boton de panico de la seccion 8.2).
"""

from __future__ import annotations

from src.decision_api.policy import PolicyEngine, PolicyRules
from src.decision_api.schemas import Decision

from .conftest import make_tx


class TestCP05UmbralesDelScore:
    """CP-05: el score cae en la banda correcta."""

    def setup_method(self) -> None:
        self.engine = PolicyEngine(PolicyRules(threshold_review=0.45, threshold_reject=0.80))

    def test_score_bajo_se_aprueba(self) -> None:
        decision, reasons = self.engine.decide(make_tx(), score=0.10)
        assert decision is Decision.APPROVE
        assert "SCORE_BELOW_REVIEW" in reasons

    def test_score_intermedio_va_a_revision(self) -> None:
        decision, reasons = self.engine.decide(make_tx(), score=0.60)
        assert decision is Decision.REVIEW
        assert "SCORE_ABOVE_REVIEW" in reasons

    def test_score_alto_se_rechaza(self) -> None:
        decision, reasons = self.engine.decide(make_tx(), score=0.95)
        assert decision is Decision.REJECT
        assert "SCORE_ABOVE_REJECT" in reasons

    def test_el_umbral_es_inclusivo(self) -> None:
        # Exactamente en el borde debe caer del lado mas severo: si el umbral
        # de rechazo es 0.80, un 0.80 se rechaza.
        assert self.engine.decide(make_tx(), score=0.80)[0] is Decision.REJECT
        assert self.engine.decide(make_tx(), score=0.45)[0] is Decision.REVIEW


class TestCP06LasReglasNoAblandan:
    """CP-06: una regla puede endurecer la decision, nunca relajarla."""

    def test_usuario_en_lista_negra_se_rechaza_con_score_bajo(self) -> None:
        engine = PolicyEngine(PolicyRules(blocked_users=frozenset({"usr_malo"})))
        decision, reasons = engine.decide(make_tx(user_token="usr_malo"), score=0.01)
        assert decision is Decision.REJECT
        assert "USER_BLOCKED" in reasons

    def test_comercio_en_lista_negra_se_rechaza(self) -> None:
        engine = PolicyEngine(PolicyRules(blocked_merchants=frozenset({"mrc_malo"})))
        decision, _ = engine.decide(make_tx(merchant_id="mrc_malo"), score=0.02)
        assert decision is Decision.REJECT

    def test_pais_bloqueado_se_rechaza(self) -> None:
        engine = PolicyEngine(PolicyRules(blocked_countries=frozenset({"XX"})))
        decision, reasons = engine.decide(make_tx(country="XX"), score=0.03)
        assert decision is Decision.REJECT
        assert "COUNTRY_BLOCKED" in reasons

    def test_monto_alto_no_se_aprueba_solo(self) -> None:
        engine = PolicyEngine(PolicyRules(amount_review_floor=1_000_000.0))
        decision, reasons = engine.decide(make_tx(amount=5_000_000.0), score=0.05)
        assert decision is Decision.REVIEW
        assert "AMOUNT_ABOVE_FLOOR" in reasons

    def test_monto_alto_no_rebaja_un_rechazo(self) -> None:
        # El piso de revision no debe convertir un REJECT en REVIEW.
        engine = PolicyEngine(PolicyRules(amount_review_floor=1_000_000.0))
        decision, _ = engine.decide(make_tx(amount=5_000_000.0), score=0.99)
        assert decision is Decision.REJECT


class TestCP07DecisionDegradada:
    """CP-07: si falto una feature, no se aprueba a ciegas (EP2 seccion 8.5)."""

    def test_degradado_sube_de_aprobar_a_revisar(self) -> None:
        engine = PolicyEngine()
        decision, reasons = engine.decide(make_tx(), score=0.10, degraded=True)
        assert decision is Decision.REVIEW
        assert "DEGRADED_TO_REVIEW" in reasons

    def test_degradado_no_cambia_un_rechazo(self) -> None:
        engine = PolicyEngine()
        decision, reasons = engine.decide(make_tx(), score=0.99, degraded=True)
        assert decision is Decision.REJECT
        assert "DEGRADED_TO_REVIEW" not in reasons


class TestCP08BotonDePanico:
    """CP-08: en rules_only el modelo deja de decidir y los pagos siguen."""

    def test_modo_solo_reglas_ignora_el_score(self) -> None:
        engine = PolicyEngine(PolicyRules(scoring_mode="rules_only"))
        decision, reasons = engine.decide(make_tx(), score=0.99)
        assert decision is Decision.APPROVE
        assert "RULES_ONLY_MODE" in reasons
        assert "SCORE_ABOVE_REJECT" not in reasons

    def test_modo_solo_reglas_mantiene_las_listas_negras(self) -> None:
        # Apagar el modelo no es apagar los controles.
        engine = PolicyEngine(
            PolicyRules(scoring_mode="rules_only", blocked_users=frozenset({"usr_malo"}))
        )
        decision, _ = engine.decide(make_tx(user_token="usr_malo"), score=0.01)
        assert decision is Decision.REJECT
