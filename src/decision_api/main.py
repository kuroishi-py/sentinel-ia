"""decision-api: la API de scoring de SENTINEL-IA.

Implementa ScoreRouter de la EP2 (seccion 5.2): orquesta la solicitud, corta al
presupuesto interno de latencia con una decision degradada y responde.

Endpoints:
    POST /v1/score      decision sobre una transaccion (RF1, RF2, RF3, RF9)
    GET  /health/live   el proceso esta vivo
    GET  /health/ready  el modelo esta cargado y la tarea puede recibir trafico
    GET  /metrics       metricas en formato Prometheus
    GET  /v1/stats      resumen en JSON (p50, p95, p99, decisiones), para el dashboard
    GET  /docs          Swagger UI, y /openapi.json el contrato (pauta 1.3)

Orden del camino sincrono: autenticacion, cuota, guardrail de entrada,
idempotencia, features, modelo, reglas. Lo caro va al final, y lo que rechaza
barato va primero.
"""

from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response, Security, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from starlette.concurrency import run_in_threadpool

from .config import get_settings
from .features import FeatureAssembler, MemoryFeatureState, MerchantRisk, RedisFeatureState
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

API_KEY_HEADER = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Clave del cliente. En AWS la reemplaza el certificado mTLS de la pasarela (EP2 8.2).",
)


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
        self.api_keys = self.settings.api_keys
        if self.settings.auth_enabled and not self.api_keys:
            logger.error("AUTH_ENABLED=true sin API_KEYS: /v1/score respondera 401 a todos")

        redis_client = self._connect_redis()
        if redis_client is not None:
            self.store = redis_client
            feature_state = RedisFeatureState(redis_client)
            self.feature_store_kind = "redis"
        else:
            self.store = InMemoryStore()
            feature_state = MemoryFeatureState()
            self.feature_store_kind = "memory"
            logger.warning("Feature store en memoria del proceso: sirve para desarrollo, no para varias tareas")

        self.idempotency = IdempotencyGuard(
            self.store,
            self.settings.idempotency_ttl_s,
            on_error=lambda operation, exc: self.telemetry.store_error(),
        )
        self.scorer = ModelLoader(self.settings).load()
        risk_path = self.settings.merchant_risk_path or str(
            Path(self.settings.model_path).with_name("merchant_risk.json")
        )
        self.merchant_risk = MerchantRisk.from_file(risk_path)
        self.features = FeatureAssembler(store=feature_state, merchant_risk=self.merchant_risk)
        self.publisher = build_publisher(self.settings)
        self.telemetry.add_gauge(
            "events_published", "Eventos publicados en el bus.", lambda: getattr(self.publisher, "published_count", 0)
        )
        self.telemetry.add_gauge(
            "events_dropped", "Eventos descartados por bufer lleno.", lambda: getattr(self.publisher, "dropped", 0)
        )
        self.limiter = TokenBucketLimiter(
            rate=self.settings.rate_limit_rps,
            burst=self.settings.rate_limit_burst,
        )
        self.ready = True

    def _connect_redis(self):
        """Cliente de Redis segun FEATURE_STORE, o None para usar memoria."""
        mode = self.settings.feature_store.lower()
        if mode == "memory":
            return None
        try:
            import redis
        except ImportError:
            logger.warning("redis-py no esta instalado")
            return None

        client = redis.Redis.from_url(
            self.settings.redis_url,
            socket_timeout=self.settings.redis_timeout_ms / 1000.0,
            socket_connect_timeout=1.0,
        )
        if mode == "redis":
            # Se usa aunque no responda ahora: redis-py reconecta solo, y
            # mientras tanto las decisiones salen degradadas, no caidas.
            logger.info("Feature store: Redis en %s", self.settings.redis_url)
            return client
        for attempt in range(3):
            try:
                client.ping()
                logger.info("Feature store conectado")
                return client
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    logger.warning("Sin feature store (%s)", exc)
                time.sleep(0.2)
        return None

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


class ServerTiming:
    """Middleware ASGI que mide /v1/score completo dentro del servidor.

    Puro ASGI, no BaseHTTPMiddleware: este ultimo agrega su propia latencia.
    El tiempo va de que llega la solicitud a que sale el ultimo byte de la
    respuesta, e incluye el parseo y la validacion del JSON y la serializacion
    de la respuesta, que ``latency_ms`` no ve.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != "/v1/score":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()

        async def send_wrapper(message):
            await send(message)
            if message.get("type") == "http.response.body" and not message.get("more_body") and state is not None:
                state.telemetry.observe_server((time.perf_counter() - started) * 1000.0)

        await self.app(scope, receive, send_wrapper)


app = FastAPI(
    title="SENTINEL-IA decision-api",
    description=(
        "Scoring de fraude en tiempo real. Recibe una transaccion tokenizada y "
        "devuelve score, decision y codigos de razon en menos de 80 ms.\n\n"
        "`POST /v1/score` exige la cabecera `X-API-Key`. El resto de los endpoints "
        "son de operacion y en el despliegue quedan detras de la red interna."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(ServerTiming)


def _remote(request: Request) -> str:
    """IP del cliente; X-Forwarded-For solo si hay un proxy propio delante."""
    assert state is not None
    if state.settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _authenticate(api_key: str | None) -> str | None:
    """Nombre del cliente si la clave es valida.

    Se compara contra todas las claves con ``hmac.compare_digest`` para que el
    tiempo de respuesta no revele cuantos caracteres coinciden.
    """
    assert state is not None
    if not api_key:
        return None
    found = None
    for key, client in state.api_keys.items():
        if hmac.compare_digest(api_key.encode("utf-8"), key.encode("utf-8")):
            found = client
    return found


def _too_many(client: str) -> JSONResponse:
    assert state is not None
    state.telemetry.rate_limited()
    retry = state.limiter.retry_after(client)
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "rate limit exceeded", "retry_after_s": round(retry, 3)},
        headers={"Retry-After": str(max(1, int(retry) + 1))},
    )


@app.post(
    "/v1/score",
    response_model=ScoreResponse,
    summary="Evalua una transaccion",
    responses={
        401: {"description": "Falta la API key o no es valida"},
        422: {"description": "Esquema invalido o campo con forma de tarjeta"},
        429: {"description": "Cuota de solicitudes excedida"},
    },
)
async def score(
    request: ScoreRequest,
    http_request: Request,
    response: Response,
    api_key: str | None = Security(API_KEY_HEADER),
) -> ScoreResponse:
    assert state is not None
    started = time.perf_counter()

    # 1. Autenticacion. La cuota se aplica por cliente autenticado, que es lo
    #    que en AWS seria la identidad del certificado mTLS.
    if state.settings.auth_enabled:
        client = _authenticate(api_key)
        if client is None:
            # Tambien hay cuota para quien no se autentica: probar claves
            # tiene un limite.
            anonymous = f"anon:{_remote(http_request)}"
            if state.settings.rate_limit_enabled and not state.limiter.allow(anonymous):
                return _too_many(anonymous)  # type: ignore[return-value]
            state.telemetry.auth_rejected()
            return JSONResponse(  # type: ignore[return-value]
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "API key ausente o invalida"},
                headers={"WWW-Authenticate": "ApiKey"},
            )
    else:
        client = f"ip:{_remote(http_request)}"

    # 2. Rate limiting antes de cualquier trabajo.
    if state.settings.rate_limit_enabled and not state.limiter.allow(client):
        return _too_many(client)  # type: ignore[return-value]

    # 3. InputGuard: nada con forma de PAN entra al sistema.
    try:
        state.input_guard.check(request)
    except PanDetectedError as exc:
        state.telemetry.guard_rejected()
        return JSONResponse(  # type: ignore[return-value]
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(exc), "field": exc.field},
        )

    # 4. Idempotencia: un id_tx repetido devuelve la misma decision.
    if not state.idempotency.reserve(request.id_tx):
        previous = state.idempotency.recall(request.id_tx)
        if previous is not None:
            state.telemetry.idempotent_hit()
            response.headers["X-Idempotent-Replay"] = "true"
            return previous

    # 5. Features, modelo y reglas.
    feature_set = state.features.build(request)
    vector = feature_set.as_vector(state.settings.feature_names)

    degraded = feature_set.degraded
    try:
        if getattr(state.scorer, "blocking_io", False):
            # Backend remoto (SageMaker): la llamada de red va a un hilo para no
            # bloquear al resto de las solicitudes que atiende este proceso.
            raw_score = await run_in_threadpool(state.scorer.score, vector)
        else:
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

    # 6. Todo lo demas ocurre fuera de la ruta critica.
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
        model_loaded=state.scorer.model_version != "none",
        details={
            "backend": state.settings.scorer_backend,
            "feature_store": state.feature_store_kind,
            "merchants_with_risk": len(state.merchant_risk),
            "auth": state.settings.auth_enabled,
        },
    )


@app.get("/metrics", response_class=PlainTextResponse, summary="Metricas Prometheus")
async def metrics() -> str:
    if state is None:
        return ""
    return state.telemetry.render()


@app.get("/v1/stats", summary="Resumen del servicio en JSON")
async def stats() -> dict:
    """p50, p95 y p99 de las ultimas 10.000 decisiones, conteos y estado.

    Con varios workers cada proceso tiene su propia telemetria: esto es la de
    quien atiende la solicitud. Prometheus suma todos al hacer scrape por tarea.
    """
    if state is None:
        return {}
    snapshot = state.telemetry.snapshot()
    snapshot["model_version"] = state.scorer.model_version
    snapshot["feature_store"] = state.feature_store_kind
    return snapshot
