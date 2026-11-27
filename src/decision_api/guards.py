"""InputGuard e IdempotencyGuard (EP2 seccion 5.2).

InputGuard rechaza cualquier campo con forma de numero de tarjeta: SENTINEL-IA
esta fuera del alcance PCI-DSS porque no recibe el PAN (restriccion RT8), y si
un PAN entra por error el sistema deja de poder afirmarlo.

IdempotencyGuard garantiza que un mismo id_tx devuelva siempre la misma
decision, sin registrarla dos veces en el log inmutable.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Protocol

from .schemas import ScoreRequest, ScoreResponse

logger = logging.getLogger(__name__)

# Secuencias de 13 a 19 digitos, con o sin separadores: la forma de un PAN.
_DIGIT_RUN = re.compile(r"(?:\d[ -]?){12,18}\d")


class PanDetectedError(ValueError):
    """Un campo trae algo que pasa la validacion de Luhn."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"El campo '{field}' contiene un numero que valida como tarjeta")


def luhn_ok(digits: str) -> bool:
    """True si la cadena de digitos cumple el algoritmo de Luhn.

    Es la prueba que usan las marcas de tarjeta para descartar tipeos, y aqui
    sirve al reves: para detectar que algo es un PAN y no un identificador
    cualquiera.
    """
    clean = [int(c) for c in digits if c.isdigit()]
    if len(clean) < 13 or len(clean) > 19:
        return False
    total = 0
    # Se recorre de derecha a izquierda duplicando las posiciones pares.
    for index, digit in enumerate(reversed(clean)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


class InputGuard:
    """Valida que ningun campo de texto traiga un PAN."""

    #: Campos que se revisan. amount y timestamp no son texto libre.
    TEXT_FIELDS = ("id_tx", "user_token", "merchant_id", "device_token", "mcc")

    def check(self, request: ScoreRequest) -> None:
        """Lanza PanDetectedError si encuentra algo con forma de tarjeta."""
        for name in self.TEXT_FIELDS:
            value = getattr(request, name, None)
            if not value:
                continue
            for candidate in _DIGIT_RUN.findall(str(value)):
                if luhn_ok(candidate):
                    raise PanDetectedError(name)


class KeyValueStore(Protocol):
    """Lo minimo que IdempotencyGuard necesita de Redis o Valkey."""

    def set(self, name: str, value: str, nx: bool = ..., ex: int | None = ...) -> bool | None: ...

    def get(self, name: str) -> str | bytes | None: ...


class InMemoryStore:
    """Respaldo en memoria para pruebas y para correr sin Redis.

    No respeta el TTL, asi que se acota por cantidad: al pasar ``max_keys`` se
    descartan las claves mas antiguas. Sin el tope, una prueba de carga larga
    sin Redis haria crecer la memoria sin limite.
    """

    def __init__(self, max_keys: int = 200_000) -> None:
        self._data: dict[str, str] = {}
        self._max_keys = max_keys

    def set(self, name: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and name in self._data:
            return None
        self._data[name] = value
        while len(self._data) > self._max_keys:
            self._data.pop(next(iter(self._data)))
        return True

    def get(self, name: str) -> str | None:
        return self._data.get(name)


class IdempotencyGuard:
    """Devuelve la decision ya tomada para un id_tx repetido.

    Usa SET NX con TTL, igual que la EP2: la primera llamada gana y las
    siguientes leen el resultado guardado.

    Si el store falla (Redis lento o caido), la idempotencia se pierde para esa
    solicitud pero el pago sigue: se evalua como si fuera nueva y no se responde
    500. La prueba de carga del 26-11-2026 encontro justo eso: un timeout de
    Redis en ``reserve`` o ``remember`` tumbaba la solicitud con la decision ya
    calculada. Entre perder la idempotencia de una transaccion y cortar un pago,
    la EP2 (RNF2 y seccion 8.5) elige lo primero.
    """

    def __init__(
        self,
        store: KeyValueStore,
        ttl_s: int = 86_400,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self._store = store
        self._ttl_s = ttl_s
        self._on_error = on_error

    def _failed(self, operation: str, exc: Exception) -> None:
        logger.warning("Idempotencia sin store en %s: %s", operation, exc)
        if self._on_error is not None:
            self._on_error(operation, exc)

    @staticmethod
    def _key(id_tx: str) -> str:
        return f"idem:{id_tx}"

    def reserve(self, id_tx: str) -> bool:
        """True si este proceso es el primero en ver este id_tx.

        Si el store no responde, devuelve True: se evalua como nueva.
        """
        try:
            created = self._store.set(self._key(id_tx), "", nx=True, ex=self._ttl_s)
        except Exception as exc:  # noqa: BLE001 - el pago no se cae por el store
            self._failed("reserve", exc)
            return True
        return bool(created)

    def remember(self, response: ScoreResponse) -> None:
        """Guarda la decision para poder repetirla tal cual."""
        try:
            self._store.set(
                self._key(response.id_tx),
                response.model_dump_json(),
                nx=False,
                ex=self._ttl_s,
            )
        except Exception as exc:  # noqa: BLE001 - la decision ya esta tomada
            self._failed("remember", exc)

    def recall(self, id_tx: str) -> ScoreResponse | None:
        """Decision previa, o None si no hay nada guardado todavia."""
        try:
            raw = self._store.get(self._key(id_tx))
        except Exception as exc:  # noqa: BLE001
            self._failed("recall", exc)
            return None
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return ScoreResponse(**json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
