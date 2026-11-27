# SENTINEL-IA

Detección de fraude en tiempo real para una fintech de pagos. Califica cada
transacción antes de autorizarla y devuelve una decisión —aprobar, revisar o
rechazar— con su explicación.

**Equipo 7** · Martínez · Farenden · Peña y Lillo · Pavón
**ITY1102 Arquitectura de Sistemas de Inteligencia Artificial** · Evaluación Parcial 3

Este repositorio implementa la arquitectura diseñada en la EP1 y la EP2
(`EP2_SENTINEL-IA_arc42_v2.md`). Cada módulo apunta en sus comentarios a la
sección de ese documento que lo justifica.

| Objetivo (EP2) | Medido en local, a 116 tx/s |
|---|---|
| p99 en la pasarela < 100 ms | **4,5 ms** (k6, 3 min, 0% errores) |
| p99 interno < 30 ms | **3,6 ms** (solicitud completa en el servidor) |
| Capacidad supuesta: 120 req/s por tarea de 1 vCPU | **375 req/s** con p99 < 30 ms |
| Menos falsos positivos que el motor de reglas | 63 alertas falsas contra 6.396 del motor, en los mismos datos |

Todo en [docs/RESULTADOS.md](docs/RESULTADOS.md). El mapa pauta → evidencia
está en [docs/EVIDENCIAS.md](docs/EVIDENCIAS.md), y la autoevaluación por
criterio, en [docs/RUBRICA.md](docs/RUBRICA.md).

---

## Cómo correrlo

### Todo el sistema, con Docker (recomendado)

```bash
docker compose up -d --build
```

| Servicio | URL |
|---|---|
| API por TLS 1.3 (el camino de pagos) | <https://localhost:8443/docs> |
| Dashboard de Fraud Ops por TLS 1.3 | <https://localhost:8444> |
| API sin TLS, solo local | <http://localhost:8000/docs> |
| Dashboard sin TLS, solo local | <http://localhost:8501> |
| RabbitMQ (guest/guest) | <http://localhost:15672> |

El certificado lo firma la CA local de Caddy. El navegador va a advertir; para
que curl o Python confíen en él:

```bash
docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt reports/caddy-root.crt
curl --cacert reports/caddy-root.crt https://localhost:8443/health/ready
# En Windows, el curl del sistema (Schannel) necesita ademas --ssl-no-revoke
```

`POST /v1/score` exige la cabecera `X-API-Key`. La clave de la demo local es
`sentinel-demo-local`; para cambiarla, copia `.env.example` a `.env`.

```bash
curl --cacert reports/caddy-root.crt https://localhost:8443/v1/score \
  -H 'X-API-Key: sentinel-demo-local' -H 'content-type: application/json' \
  -d '{"id_tx":"tx-demo-0001","user_token":"usr_demo_0001","merchant_id":"mrc_0042","amount":19990,"currency":"CLP","country":"CL"}'
```

En el dashboard, la pestaña **Probar transacciones** arma un usuario con un mes
de historia y dispara una toma de cuenta o una prueba de tarjeta. Muestra cómo
decide la API y, segundos después, la explicación SHAP que llega por RabbitMQ.
Enlaces directos: `?vista=probar&escenario=ato`, `?vista=modelo` y
`?vista=casos`.

En Linux, para que el worker escriba en `data/`: `SENTINEL_UID=$(id -u) docker compose up -d`.

Si cambias `dashboard/app.py` con Docker Desktop en Windows, reinicia el
contenedor (`docker compose restart dashboard`): el montaje no avisa los
cambios y Streamlit sigue con la versión anterior.

### Solo la API, sin Docker

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

FEATURE_STORE=memory PUBLISHER_ENABLED=false API_KEYS=local:clave-local \
  uvicorn src.decision_api.main:app --reload
```

Sin Redis, el feature store queda en memoria del proceso; sin RabbitMQ no hay
explicaciones. El modelo (`artifacts/model.json`) viene en el repositorio.

---

## Pruebas

```bash
pytest                                  # 95 unitarias y de integracion (CP-01 a CP-22)
pytest --cov=src --cov-report=term      # con cobertura
ruff check src tests load dashboard scripts

# con el sistema levantado: interoperabilidad de punta a punta (CP-23)
docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt reports/caddy-root.crt
SENTINEL_E2E_URL=https://localhost:8443 pytest -m e2e -v

python scripts/verificar_seguridad.py   # TLS 1.3, API key, endpoints ocultos, HSTS
```

### Informe en PDF

```bash
python scripts/generar_informe.py       # docs/informe/EP3_Equipo7_...pdf, 10 paginas
```

El PDF de entrega está en
[docs/informe/](docs/informe/EP3_Equipo7_Martinez_Farenden_PenayLillo_Pavon.pdf).
El script lo regenera desde `docs/informe/EP3_informe_tecnico.md` con Chrome o
Edge headless, e informa cuántas páginas tiene y cuántos `[COMPLETAR]` quedan.
Los datos de la portada salen de `docs/informe/portada.local.json`, que no se
versiona: sin ese archivo, la portada queda con `[COMPLETAR]` (ver el
docstring del script).

Son 24 casos (CP-01 a CP-23, más CP-20b) en 101 funciones; la tabla está en
[docs/EVIDENCIAS.md](docs/EVIDENCIAS.md#23-testing-y-calidad--ie10). Si hay un
Redis en `REDIS_URL`, CP-20 compara además el feature store en Redis contra el
de memoria. La CI (`.github/workflows/ci.yml`) corre ruff y pytest con Redis,
controla que el modelo versionado corresponda al entrenamiento y levanta la
imagen.

### Carga

```bash
docker compose exec api python -m src.demo.seed --users 20000      # usuarios con historia
docker compose --profile carga run --rm k6                          # 116 tx/s, 3 min
docker compose exec -T dashboard python - < load/sonda_conexiones.py  # sin el cliente de Locust
PERFIL=pico docker compose --profile carga run --rm locust          # lo mismo con Locust
PERFIL=capacidad DURACION=1m docker compose --profile carga run --rm locust
```

La carga corre dentro de la red de Docker, contra la tarea, como el
balanceador. La precarga hace falta: sin historia, cada solicitud parece una
cuenta nueva desde un dispositivo nuevo y el modelo la manda a revisión. Por
qué k6 además de Locust:
[RESULTADOS.md §2.1](docs/RESULTADOS.md#21-pico-del-caso-116-txs-durante-3-minutos).

---

## El modelo

Hay dos, a propósito (issue #1):

- **Modelo de servicio** (`artifacts/`): XGBoost con las 11 features del diseño
  (velocidad 1 h/24 h, monto frente al promedio del usuario, dispositivo nuevo,
  riesgo del comercio...).
  - Responde `/v1/score` y se explica en el dashboard.
  - Se entrena con una historia de transacciones simulada
    (`src/training/simulate.py`) cuyas features calcula el mismo código que la
    API, así que entrenamiento y servicio no pueden divergir (RT2).
  - Usa restricciones monotónicas contra la evasión.
  - En los 14 días de prueba: precision 0,87, recall 0,83 y 0,09% de falsos
    positivos sobre transacciones legítimas.
- **Benchmark real** (`artifacts/benchmark_ulb/`): el mismo algoritmo sobre las
  284.807 transacciones reales de la ULB. Precision 0,87 y recall 0,84. Sus
  features son componentes PCA: sirve para medir, no para servir ni para
  explicar.

```bash
python -m src.training.train --simulated --model-version v1.0.0-sim   # reentrena el de servicio
python -m src.training.download_dataset                                # benchmark: baja la ULB
python -m src.training.train --data data/creditcard.csv --drop Time --out artifacts/benchmark_ulb
```

---

## Estructura

```
src/decision_api/      La API de scoring (ScoreRouter de la EP2, seccion 5.2)
  main.py              Orden del camino sincrono: clave, cuota, guardrail, idempotencia, features, modelo, reglas
  features.py          compute_features (pura) + feature store en Redis o en memoria
  guards.py            InputGuard (rechazo de PAN por Luhn) e IdempotencyGuard tolerante a fallas
  scorer.py            ModelLoader (SHA-256, warm-up, 1 hilo) y backends local y SageMaker
  policy.py            PolicyEngine: umbrales, listas negras, boton de panico
  ratelimit.py         Token bucket por cliente
  publisher.py         Publicacion asincrona a RabbitMQ, nunca bloquea el pago
  telemetry.py         /metrics, /v1/stats y percentiles p50, p95, p99
src/shap_worker/       Consume decision.v1, calcula TreeSHAP y crea el caso
src/training/          Simulador, entrenamiento, benchmark ULB y despliegue en SageMaker
src/demo/              Escenarios de demostracion y precarga para la prueba de carga
dashboard/             Consola de Fraud Ops (Streamlit)
deploy/Caddyfile       Borde TLS 1.3 (el papel del ALB en la EP2)
load/                  Locust y k6
scripts/               Verificacion de seguridad del borde y generador del informe en PDF
tests/                 24 casos de prueba, 101 funciones
docs/                  RESULTADOS, EVIDENCIAS, RUBRICA, diagramas PlantUML, capturas e informe
```

---

## Decisiones que conviene leer antes de tocar el código

**El modelo vive en dos lugares.** La EP2 decidió (D2) que va dentro de la API,
en memoria; la pauta de la EP3 pide desplegarlo en SageMaker. `scorer.py`
implementa los dos backends con la misma interfaz
(`SCORER_BACKEND=local|sagemaker`) para medir la diferencia. El lado en memoria
está medido (p99 de 4,5 ms). Para el de SageMaker, ver la nota del issue #3 en
[docs/EVIDENCIAS.md](docs/EVIDENCIAS.md).

**RabbitMQ en la demo, Kinesis en el diseño.** La pauta nombra RabbitMQ o
Kafka. `publisher.py` aísla el transporte, así que cambiarlo no toca la API.

**Autenticación por API key en local, mTLS en AWS.** En la EP2 la pasarela se
autentica con certificado en el ALB. En local, Caddy termina TLS 1.3 y la API
exige `X-API-Key`. La cuota se aplica por cliente autenticado, que es lo que en
AWS sería la identidad del certificado.

**Si algo falla, se degrada; no se cae.** Si falla Redis, la decisión sale
degradada y nunca se aprueba a ciegas. Si falla la idempotencia, se evalúa como
nueva. Si falla RabbitMQ, el evento queda en un búfer. Si falta el modelo, todo
va a revisión.

---

## Configuración

Todo por variables de entorno; ningún secreto en el código ni en la imagen
(EP2 sección 8.2). Plantilla comentada en `.env.example`.

| Variable | Por omisión | Para qué |
|---|---|---|
| `API_KEYS` | — | Claves de clientes, `cliente:clave,...`. Sin claves, `/v1/score` responde 401 |
| `FEATURE_STORE` | `auto` | `redis`, `memory` o `auto` (Redis si responde) |
| `SCORER_BACKEND` | `local` | `local` o `sagemaker` |
| `MODEL_PATH` | `artifacts/model.json` | Artefacto del modelo, verificado por SHA-256 |
| `REDIS_URL` / `REDIS_TIMEOUT_MS` | `redis://localhost:6379/0` / `50` | Feature store e idempotencia |
| `AMQP_URL` | `amqp://guest:guest@localhost:5672/` | Bus de eventos |
| `INTERNAL_BUDGET_MS` | `60` | Corte interno antes del timeout de 80 ms |
| `THRESHOLD_REVIEW` / `THRESHOLD_REJECT` | `0.45` / `0.80` | Umbrales de la política |
| `RATE_LIMIT_RPS` / `RATE_LIMIT_BURST` | `200` / `400` | Cuota por cliente y por proceso |
| `TRUST_PROXY_HEADERS` | `false` | Usar `X-Forwarded-For` solo detrás de un proxy propio |

---

## Fuera de alcance de este repositorio

El despliegue en AWS (endpoint de SageMaker, CloudWatch, URL pública) no está
ejecutado. Lo que falta y cómo hacerlo está en
[docs/EVIDENCIAS.md](docs/EVIDENCIAS.md). **Si lo despliegan, borren el
endpoint al terminar:** uno olvidado consume crédito todos los días.
