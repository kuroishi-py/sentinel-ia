"""decision-api: la API de scoring de SENTINEL-IA.

Implementa ScoreRouter de la EP2 (seccion 5.2): orquesta la solicitud, corta al
presupuesto interno de latencia con una decision degradada y responde.

Endpoints:
    POST /v1/score      decision sobre una transaccion (RF1, RF2, RF3, RF9)
    GET  /health/live   el proceso esta vivo
    GET  /health/ready  el modelo esta cargado y la tarea puede recibir trafico
    GET  /metrics       metricas en formato Prometheus
    GET  /docs          Swagger UI, y /openapi.json el contrato (pauta 1.3)
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import get_settings
from .features import FeatureAssembler
from .guards import IdempotencyGuard, InMemoryStore, InputGuard, PanDetectedError
from .policy import PolicyEngine, PolicyRules
from .publisher import build_publisher
from .ratelimit import TokenBucketLimiter
from .schemas import DecisionEvent, HealthStatus, ScoreRequest, ScoreResponse
from .scorer import ModelLoader
from .telemetry import Telemetry

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
logger = logging.getLogger("decision_api")


class AppState:
    """Todo lo que la API necesita tener listo antes de recibir trafico."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.input_guard = InputGuard()
        self.policy = PolicyEngine(
            PolicyRules(
                threshold_review=self.settings.threshold_review,
                threshold_reject=self.settings.threshold_reject,
                amount_review_floor=self.settings.amount_review_floor,
            )
        )
        self.telemetry = Telemetry()
        self.store = self._connect_store()
        self.idempotency = IdempotencyGuard(self.store, self.settings.idempotency_ttl_s)
        self.features = FeatureAssembler(store=self._redis_or_none())
        self.scorer = ModelLoader(self.settings).load()
        self.publisher = build_publisher(self.settings)
        self.limiter = TokenBucketLimiter(
            rate=self.settings.rate_limit_rps,
            burst=self.settings.rate_limit_burst,
        )
        self.ready = True

    def _connect_store(self):
        try:
            import redis

            client = redis.Redis.from_url(self.settings.redis_url, socket_timeout=0.05)
            client.ping()
            logger.info("Feature store conectado")
            return client
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sin feature store (%s); se usa memoria local", exc)
            return InMemoryStore()

    def _redis_or_none(self):
        return self.store if not isinstance(self.store, InMemoryStore) else None

    def close(self) -> None:
        self.publisher.close()


state: AppState | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global state
    state = AppState()
    logger.info("decision-api listo, modelo %s", state.scorer.model_version)
    yield
    if state is not None:
        state.close()


app = FastAPI(
    title="SENTINEL-IA decision-api",
    description=(
        "Scoring de fraude en tiempo real. Recibe una transaccion tokenizada y "
        "devuelve score, decision y codigos de razon en menos de 80 ms."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


def _client_id(request: Request) -> str:
    """Identidad para el rate limiting.

    En produccion la pasarela se autentica con mTLS y su certificado es la
    identidad; detras del ALB se usa el primer valor de X-Forwarded-For.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post(
    "/v1/score",
    response_model=ScoreResponse,
    summary="Evalua una transaccion",
    responses={
        422: {"description": "Esquema invalido o campo con forma de tarjeta"},
        429: {"description": "Cuota de solicitudes excedida"},
    },
)
async def score(request: ScoreRequest, http_request: Request, response: Response) -> ScoreResponse:
    assert state is not None
    started = time.perf_counter()

    # 1. Rate limiting antes de cualquier trabajo.
    if state.settings.rate_limit_enabled:
        client = _client_id(http_request)
        if not state.limiter.allow(client):
            state.telemetry.rate_limited()
            retry = state.limiter.retry_after(client)
            return JSONResponse(  # type: ignore[return-value]
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "rate limit exceeded", "retry_after_s": round(retry, 3)},
                headers={"Retry-After": str(max(1, int(retry) + 1))},
            )

    # 2. InputGuard: nada con forma de PAN entra al sistema.
    try:
        state.input_guard.check(request)
    except PanDetectedError as exc:
        state.telemetry.guard_rejected()
        return JSONResponse(  # type: ignore[return-value]
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(exc), "field": exc.field},
        )

    # 3. Idempotencia: un id_tx repetido devuelve la misma decision.
    if not state.idempotency.reserve(request.id_tx):
        previous = state.idempotency.recall(request.id_tx)
        if previous is not None:
            state.telemetry.idempotent_hit()
            response.headers["X-Idempotent-Replay"] = "true"
            return previous

    # 4. Features, modelo y reglas.
    feature_set = state.features.build(request)
    vector = feature_set.as_vector(state.settings.feature_names)

    degraded = feature_set.degraded
    try:
        raw_score = state.scorer.score(vector)
    except Exception as exc:  # noqa: BLE001 - el pago no se cae por el modelo
        logger.error("Fallo el scoring de %s: %s", request.id_tx, exc)
        raw_score = 0.5
        degraded = True

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if elapsed_ms > state.settings.internal_budget_ms:
        # Se agoto el presupuesto interno: se responde degradado antes de que
        # la pasarela corte a los 80 ms (EP2 RF9).
        degraded = True
        state.telemetry.budget_exceeded()

    decision, reasons = state.policy.decide(request, raw_score, degraded=degraded)

    result = ScoreResponse(
        id_tx=request.id_tx,
        score=round(float(raw_score), 6),
        decision=decision,
        model_version=state.scorer.model_version,
        reason_codes=reasons,
        degraded=degraded,
        latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )

    # 5. Todo lo demas ocurre fuera de la ruta critica.
    state.idempotency.remember(result)
    state.publisher.publish(
        DecisionEvent(
            id_tx=request.id_tx,
            user_token=request.user_token,
            merchant_id=request.merchant_id,
            amount=request.amount,
            currency=request.currency,
            score=result.score,
            decision=decision,
            model_version=result.model_version,
            reason_codes=reasons,
            degraded=degraded,
            features=feature_set.values,
            decided_at=datetime.now(UTC),
        )
    )
    state.telemetry.observe(decision, result.latency_ms, degraded)
    return result


@app.get("/health/live", response_model=HealthStatus, summary="El proceso responde")
async def health_live() -> HealthStatus:
    return HealthStatus(status="alive")


@app.get("/health/ready", response_model=HealthStatus, summary="La tarea puede recibir trafico")
async def health_ready(response: Response) -> HealthStatus:
    # El ALB usa "menos solicitudes pendientes", que no admite slow start, asi
    # que /health/ready solo responde 200 con el modelo ya cargado (EP2 7.3).
    if state is None or not state.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthStatus(status="loading", model_loaded=False)
    return HealthStatus(
        status="ready",
        model_version=state.scorer.model_version,
        model_loaded=True,
        details={"backend": state.settings.scorer_backend},
    )


@app.get("/metrics", response_class=PlainTextResponse, summary="Metricas Prometheus")
async def metrics() -> str:
    if state is None:
        return ""
    return state.telemetry.render()
