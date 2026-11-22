"""Casos CP-01 a CP-04: guardrails de entrada e idempotencia.

Trazabilidad: EP2 seccion 5.2 (InputGuard, IdempotencyGuard), restriccion RT8,
amenaza AML.T0010 y escenario de calidad "Analizabilidad".
"""

from __future__ import annotations

import pytest

from src.decision_api.guards import (
    IdempotencyGuard,
    InMemoryStore,
    InputGuard,
    PanDetectedError,
    luhn_ok,
)
from src.decision_api.schemas import Decision, ScoreResponse

from .conftest import make_tx


class TestCP01LuhnDetectaTarjetas:
    """CP-01: la validacion de Luhn distingue un PAN de un identificador."""

    @pytest.mark.parametrize(
        "number",
        [
            "4111111111111111",  # Visa de prueba
            "5500005555555559",  # Mastercard de prueba
            "4000056655665556",
            "378282246310005",  # Amex, 15 digitos
        ],
    )
    def test_numeros_de_tarjeta_validan(self, number: str) -> None:
        assert luhn_ok(number) is True

    @pytest.mark.parametrize(
        "number",
        [
            "4111111111111112",  # un digito cambiado
            "1234567890123",
            "0000000000000001",
        ],
    )
    def test_numeros_cualquiera_no_validan(self, number: str) -> None:
        assert luhn_ok(number) is False

    @pytest.mark.parametrize("number", ["411111111111", "41111111111111111111", "", "abcd"])
    def test_largos_fuera_de_rango_se_descartan(self, number: str) -> None:
        # Un PAN tiene entre 13 y 19 digitos: fuera de ahi no es una tarjeta.
        assert luhn_ok(number) is False


class TestCP02InputGuardRechazaPAN:
    """CP-02: ningun campo puede traer un numero de tarjeta (RT8)."""

    def test_transaccion_limpia_pasa(self) -> None:
        InputGuard().check(make_tx())

    def test_pan_en_el_token_de_usuario_se_rechaza(self) -> None:
        with pytest.raises(PanDetectedError) as exc:
            InputGuard().check(make_tx(user_token="4111111111111111"))
        assert exc.value.field == "user_token"

    def test_pan_con_separadores_tambien_se_detecta(self) -> None:
        # Un PAN formateado sigue siendo un PAN.
        with pytest.raises(PanDetectedError):
            InputGuard().check(make_tx(user_token="4111-1111-1111-1111"))

    def test_pan_escondido_dentro_de_otro_texto(self) -> None:
        with pytest.raises(PanDetectedError) as exc:
            InputGuard().check(make_tx(merchant_id="mrc_4111111111111111_x"))
        assert exc.value.field == "merchant_id"

    def test_identificador_largo_sin_luhn_no_molesta(self) -> None:
        # Falso positivo que si ocurriera bloquearia transacciones legitimas.
        InputGuard().check(make_tx(user_token="usr_1234567890123456"))


class TestCP03IdempotenciaDevuelveLaMismaDecision:
    """CP-03: un mismo id_tx devuelve la misma decision y se registra una vez."""

    def _response(self, decision: Decision = Decision.REJECT) -> ScoreResponse:
        return ScoreResponse(
            id_tx="tx-00000001",
            score=0.91,
            decision=decision,
            model_version="v1.2.0",
            reason_codes=["SCORE_ABOVE_REJECT"],
            degraded=False,
            latency_ms=12.5,
        )

    def test_la_primera_llamada_reserva_y_la_segunda_no(self) -> None:
        guard = IdempotencyGuard(InMemoryStore())
        assert guard.reserve("tx-00000001") is True
        assert guard.reserve("tx-00000001") is False

    def test_la_decision_guardada_se_recupera_identica(self) -> None:
        guard = IdempotencyGuard(InMemoryStore())
        guard.reserve("tx-00000001")
        original = self._response()
        guard.remember(original)

        recovered = guard.recall("tx-00000001")
        assert recovered is not None
        assert recovered.decision is Decision.REJECT
        assert recovered.score == original.score
        assert recovered.model_version == original.model_version
        assert recovered.reason_codes == original.reason_codes

    def test_sin_decision_previa_devuelve_none(self) -> None:
        guard = IdempotencyGuard(InMemoryStore())
        assert guard.recall("tx-inexistente") is None

    def test_ids_distintos_no_se_pisan(self) -> None:
        guard = IdempotencyGuard(InMemoryStore())
        guard.reserve("tx-A")
        assert guard.reserve("tx-B") is True


class TestCP04IdempotenciaToleraDatosCorruptos:
    """CP-04: un valor ilegible en el store no debe voltear el servicio."""

    def test_json_invalido_se_trata_como_ausente(self) -> None:
        store = InMemoryStore()
        store.set("idem:tx-roto", "{esto no es json")
        guard = IdempotencyGuard(store)
        assert guard.recall("tx-roto") is None
