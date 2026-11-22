"""PolicyEngine (EP2 seccion 5.2).

Las reglas son el piso del sistema: el modelo puede equivocarse o quedar
desactualizado, pero una tarjeta en lista negra se rechaza igual. Por eso las
reglas se evaluan despues del score y pueden endurecer la decision, nunca
ablandarla.

En produccion estas reglas viven en AWS AppConfig y cambian sin desplegar
(EP2 D10, RNF6). Aqui se reciben como argumentos para que sean probables.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schemas import Decision, ScoreRequest


@dataclass(frozen=True)
class PolicyRules:
    """Umbrales y listas vigentes."""

    threshold_review: float = 0.45
    threshold_reject: float = 0.80
    amount_review_floor: float = 2_000_000.0
    blocked_users: frozenset[str] = field(default_factory=frozenset)
    blocked_merchants: frozenset[str] = field(default_factory=frozenset)
    blocked_countries: frozenset[str] = field(default_factory=frozenset)
    #: Modo de operacion: "model" normal, "rules_only" es el boton de panico
    #: de la EP2 seccion 8.2, que deja de usar el modelo sin detener los pagos.
    scoring_mode: str = "model"


#: Severidad de cada decision. Una regla solo puede subir por esta escala.
_SEVERITY = {Decision.APPROVE: 0, Decision.REVIEW: 1, Decision.REJECT: 2}


class PolicyEngine:
    """Convierte un score en una decision y agrega los codigos de razon."""

    def __init__(self, rules: PolicyRules | None = None) -> None:
        self._rules = rules or PolicyRules()

    @property
    def rules(self) -> PolicyRules:
        return self._rules

    def decide(
        self,
        request: ScoreRequest,
        score: float,
        degraded: bool = False,
    ) -> tuple[Decision, list[str]]:
        """Decision final y codigos de razon, en orden de aparicion."""
        rules = self._rules
        reasons: list[str] = []

        # Boton de panico: el modelo no decide, solo las reglas.
        if rules.scoring_mode == "rules_only":
            decision = Decision.APPROVE
            reasons.append("RULES_ONLY_MODE")
        else:
            decision = self._from_score(score, reasons)

        # Las listas negras son absolutas.
        if request.user_token in rules.blocked_users:
            decision = self._escalate(decision, Decision.REJECT)
            reasons.append("USER_BLOCKED")
        if request.merchant_id in rules.blocked_merchants:
            decision = self._escalate(decision, Decision.REJECT)
            reasons.append("MERCHANT_BLOCKED")
        if request.country in rules.blocked_countries:
            decision = self._escalate(decision, Decision.REJECT)
            reasons.append("COUNTRY_BLOCKED")

        # Un monto alto nunca se aprueba solo: pasa a revision humana.
        if request.amount >= rules.amount_review_floor:
            decision = self._escalate(decision, Decision.REVIEW)
            reasons.append("AMOUNT_ABOVE_FLOOR")

        # Si falto una feature, aprobar a ciegas es lo peor que se puede hacer.
        if degraded and decision is Decision.APPROVE:
            decision = Decision.REVIEW
            reasons.append("DEGRADED_TO_REVIEW")

        return decision, reasons

    def _from_score(self, score: float, reasons: list[str]) -> Decision:
        rules = self._rules
        if score >= rules.threshold_reject:
            reasons.append("SCORE_ABOVE_REJECT")
            return Decision.REJECT
        if score >= rules.threshold_review:
            reasons.append("SCORE_ABOVE_REVIEW")
            return Decision.REVIEW
        reasons.append("SCORE_BELOW_REVIEW")
        return Decision.APPROVE

    @staticmethod
    def _escalate(current: Decision, candidate: Decision) -> Decision:
        """Devuelve la mas severa de las dos. Las reglas no ablandan."""
        return candidate if _SEVERITY[candidate] > _SEVERITY[current] else current
