"""DecisionPublisher (EP2 seccion 5.2).

Publica cada decision en el bus para que el log inmutable, SHAP, los casos y la
evaluacion en sombra ocurran fuera de la ruta critica del pago.

La EP2 eligio Kinesis (D15); la pauta de la EP3 pide RabbitMQ o Kafka, asi que
el tramo que se demuestra usa RabbitMQ. La interfaz es la misma, de modo que
cambiar de transporte no toca a ``decision-api``.

Regla de oro: publicar NUNCA bloquea la respuesta al pago. Si el bus no
responde, el evento queda en un bufer en memoria y se reintenta; si el bufer se
llena, se descartan los mas antiguos y se incrementa un contador que tiene
alerta propia (EP2 seccion 8.3, "eventos no registrados").
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
from typing import Protocol

from .schemas import DecisionEvent

logger = logging.getLogger(__name__)

#: Capacidad del bufer. La EP2 dimensiona 10.000 eventos, unos 80 s de pico.
BUFFER_MAXSIZE = 10_000


class Publisher(Protocol):
    def publish(self, event: DecisionEvent) -> None: ...

    def close(self) -> None: ...


class NullPublisher:
    """No publica nada. Para pruebas unitarias y para correr sin bus."""

    def __init__(self) -> None:
        self.published: list[DecisionEvent] = []

    def publish(self, event: DecisionEvent) -> None:
        self.published.append(event)

    def close(self) -> None:
        pass


class RabbitMQPublisher:
    """Publica en RabbitMQ desde un hilo aparte.

    El hilo es lo que hace que publicar no sume latencia: ``publish`` solo
    encola y vuelve de inmediato.
    """

    def __init__(self, amqp_url: str, exchange: str) -> None:
        self._amqp_url = amqp_url
        self._exchange = exchange
        self._queue: queue.Queue[DecisionEvent | None] = queue.Queue(maxsize=BUFFER_MAXSIZE)
        self._dropped = 0
        self._published = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="decision-publisher", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        """Eventos perdidos por bufer lleno. Tiene alerta propia."""
        return self._dropped

    @property
    def published_count(self) -> int:
        return self._published

    def publish(self, event: DecisionEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Se descarta el mas antiguo: un evento viejo vale menos que el
            # actual, y el contador deja el rastro para la alerta.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except (queue.Empty, queue.Full):
                pass
            self._dropped += 1
            logger.error("Bufer de decisiones lleno; %d eventos descartados", self._dropped)

    def _run(self) -> None:
        channel = None
        connection = None
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                break
            try:
                if channel is None:
                    connection, channel = self._connect()
                channel.basic_publish(
                    exchange=self._exchange,
                    routing_key=event.user_token,  # particion por usuario (EP2 5.1)
                    body=json.dumps(event.model_dump(mode="json")).encode("utf-8"),
                )
                self._published += 1
            except Exception as exc:  # noqa: BLE001 - se reintenta con la conexion nueva
                logger.warning("Fallo al publicar %s: %s", event.id_tx, exc)
                channel = None
                connection = None
                # Se devuelve a la cola para no perder el evento.
                try:
                    self._queue.put_nowait(event)
                except queue.Full:
                    self._dropped += 1
                self._stop.wait(1.0)

        if connection is not None:
            # Al cerrar ya no importa si la conexion estaba rota.
            with contextlib.suppress(Exception):
                connection.close()

    def _connect(self):
        import pika  # local: solo se necesita con este backend

        params = pika.URLParameters(self._amqp_url)
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        channel.exchange_declare(exchange=self._exchange, exchange_type="topic", durable=True)
        logger.info("Publisher conectado a %s, exchange %s", params.host, self._exchange)
        return connection, channel

    def close(self) -> None:
        self._stop.set()
        # El centinela solo apura la salida; si la cola esta llena, el hilo
        # igual termina por el timeout de su propio get.
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=3.0)


def build_publisher(settings) -> Publisher:
    """Publisher segun configuracion, con respaldo silencioso."""
    if not settings.publisher_enabled:
        logger.info("Publisher deshabilitado (PUBLISHER_ENABLED=false)")
        return NullPublisher()
    try:
        return RabbitMQPublisher(settings.amqp_url, settings.decision_exchange)
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo iniciar el publisher de RabbitMQ: %s", exc)
        return NullPublisher()
