"""InputGuard e IdempotencyGuard (EP2 seccion 5.2).

InputGuard rechaza cualquier campo con forma de numero de tarjeta: SENTINEL-IA
esta fuera del alcance PCI-DSS porque no recibe el PAN (restriccion RT8), y si
un PAN entra por error el sistema deja de poder afirmarlo.

IdempotencyGuard garantiza que un mismo id_tx devuelva siempre la misma
decision, sin registrarla dos veces en el log inmutable.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from .schemas import ScoreRequest, ScoreResponse

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
    """Respaldo en memoria para pruebas y para correr sin Redis."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def set(self, name: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and name in self._data:
            return None
        self._data[name] = value
        return True

    def get(self, name: str) -> str | None:
        return self._data.get(name)


class IdempotencyGuard:
    """Devuelve la decision ya tomada para un id_tx repetido.

    Usa SET NX con TTL, igual que la EP2: la primera llamada gana y las
    siguientes leen el resultado guardado.
    """

    def __init__(self, store: KeyValueStore, ttl_s: int = 86_400) -> None:
        self._store = store
        self._ttl_s = ttl_s

    @staticmethod
    def _key(id_tx: str) -> str:
        return f"idem:{id_tx}"

    def reserve(self, id_tx: str) -> bool:
        """True si este proceso es el primero en ver este id_tx."""
        created = self._store.set(self._key(id_tx), "", nx=True, ex=self._ttl_s)
        return bool(created)

    def remember(self, response: ScoreResponse) -> None:
        """Guarda la decision para poder repetirla tal cual."""
        self._store.set(
            self._key(response.id_tx),
            response.model_dump_json(),
            nx=False,
            ex=self._ttl_s,
        )

    def recall(self, id_tx: str) -> ScoreResponse | None:
        """Decision previa, o None si no hay nada guardado todavia."""
        raw = self._store.get(self._key(id_tx))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return ScoreResponse(**json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
