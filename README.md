# SENTINEL-IA

Detección de fraude en tiempo real para una fintech de pagos. Califica cada
transacción antes de autorizarla y devuelve una decisión —aprobar, revisar o
rechazar— con su explicación.

**Equipo 7** · Martínez · Farenden · Peña y Lillo · Pavón
**ITY1102 Arquitectura de Sistemas de Inteligencia Artificial** · Evaluación Parcial 3

Este repositorio es la implementación de la arquitectura diseñada en la EP1 y la
EP2 (`EP2_SENTINEL-IA_arc42_v2.md`). Cada módulo apunta en sus comentarios a la
sección de ese documento que lo justifica.

---

## El problema

Una fintech con 200.000 usuarios y 200.000 transacciones diarias opera un motor
de reglas con **40% de falsos positivos** y pierde **USD 500.000 anuales** en
fraude no contenido. SENTINEL-IA lo reemplaza por un modelo que califica cada
transacción en menos de 100 ms, explica cada rechazo y escala a 1.000.000 de
transacciones diarias sin rediseño.

| Objetivo | Valor |
|---|---|
| Latencia p99 en la pasarela | < 100 ms |
| Latencia p99 interna | < 30 ms |
| Disponibilidad | 99,9% mensual |
| Pico de carga | 116 tx/s |
| Falsos positivos | menos que el 40% actual, sin perder recall |

---

## Cómo correrlo

### Local, sin Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

Entrena un modelo. Con `--synthetic` se genera un dataset con el desbalance del
caso (0,5% de fraude), suficiente para verificar que todo el pipeline corre:

```bash
python -m src.training.train --synthetic
```

Levanta la API:

```bash
uvicorn src.decision_api.main:app --reload
```

El contrato queda en <http://localhost:8000/docs>. Una transacción de prueba:

```bash
curl -X POST http://localhost:8000/v1/score -H 'content-type: application/json' -d '{"id_tx":"tx-00000001","user_token":"usr_demo","merchant_id":"mrc_0042","amount":19990,"currency":"CLP","country":"CL"}'
```

### Local, con Docker

Levanta API, Redis, RabbitMQ, el worker de SHAP y el dashboard:

```bash
docker compose up -d
```

| Servicio | URL |
|---|---|
| API y Swagger | <http://localhost:8000/docs> |
| Dashboard de Fraud Ops | <http://localhost:8501> |
| RabbitMQ (guest/guest) | <http://localhost:15672> |
| Métricas Prometheus | <http://localhost:8000/metrics> |

### Con el dataset real

Para el informe hay que usar datos reales, no sintéticos. Descarga
`creditcard.csv` (Credit Card Fraud Detection, ULB — 284.807 transacciones,
0,17% de fraude) a `data/` y entrena:

```bash
python -m src.training.train --data data/creditcard.csv --label Class
```

---

## Estructura

```
src/decision_api/      La API de scoring (ScoreRouter de la EP2, sección 5.2)
  schemas.py           Contrato POST /v1/score y evento decision.v1
  guards.py            InputGuard (rechazo de PAN por Luhn) e IdempotencyGuard
  features.py          FeatureAssembler: perfil y contadores en una sola llamada
  scorer.py            ModelScorer y ModelLoader, backends local y SageMaker
  policy.py            PolicyEngine: umbrales, listas negras, botón de pánico
  ratelimit.py         Token bucket por cliente
  publisher.py         Publicación asíncrona a RabbitMQ, nunca bloquea el pago
  telemetry.py         /metrics y percentiles p50, p95, p99
src/shap_worker/       Consume decision.v1, calcula TreeSHAP y crea el caso
src/training/          Entrenamiento local y despliegue en SageMaker
dashboard/             Consola de Fraud Ops (Streamlit)
tests/                 17 casos de prueba (64 funciones), unitarios y de integración
load/                  Prueba de carga con Locust
docs/EVIDENCIAS.md     Mapa de cada punto de la pauta a su evidencia
```

---

## Dos decisiones que conviene leer antes de tocar el código

### El modelo vive en dos lugares, a propósito

La EP2 decidió (D2) que el modelo va **dentro** de la API, en memoria, porque un
endpoint de SageMaker agrega un salto de red que se come el presupuesto de
latencia. La pauta de la EP3 exige desplegar el modelo **en SageMaker**, con
endpoints configurados.

No es una contradicción que haya que esconder: es un resultado que se puede
medir. `scorer.py` implementa los dos backends con la misma interfaz:

```bash
SCORER_BACKEND=local                                   # modelo en memoria
SCORER_BACKEND=sagemaker SAGEMAKER_ENDPOINT=<nombre>   # endpoint de SageMaker
```

Corre `load/locustfile.py` contra los dos y pon las tablas de percentiles una al
lado de la otra. Eso convierte el supuesto de la EP2 en evidencia, que es
exactamente lo que la pauta pide cuando dice «justifican decisiones de
implementación y resultados obtenidos».

### El bus de eventos es RabbitMQ en la demo y Kinesis en el diseño

La EP2 eligió Kinesis (D15) por el reproceso de 7 días y el orden por usuario.
La pauta de la EP3 nombra RabbitMQ o Kafka. El tramo asíncrono que se demuestra
usa RabbitMQ porque es casi gratis y se levanta en un contenedor;
`publisher.py` aísla el transporte, así que cambiarlo no toca a `decision-api`.
El informe mantiene Kinesis como la elección de diseño y la comparación entre
ambos ya está escrita en la EP2.

---

## Pruebas

```bash
pytest                              # 64 funciones, 17 casos
pytest --cov=src --cov-report=term  # con cobertura
pytest tests/test_policy.py -v      # un grupo
```

Los casos están numerados CP-01 a CP-17 —uno por clase de prueba, 64 funciones
en total— y cada clase dice en su docstring qué sección de la EP2 o de la pauta
cubre, para que la trazabilidad del informe sea copiar y pegar.

Estado al 22-11-2026: **64 pasan, 0 fallan**, y `ruff check` sin hallazgos.

### Carga

```bash
# interfaz web
locust -f load/locustfile.py --host http://localhost:8000

# 116 usuarios (el pico del caso) por 3 minutos, con CSV para el informe
locust -f load/locustfile.py --host http://localhost:8000 \
       --headless -u 116 -r 20 -t 3m --csv reports/carga
```

---

## Despliegue en AWS

```bash
pip install boto3 sagemaker
aws configure   # o variables de entorno

python -m src.training.sagemaker_deploy \
  --role-arn arn:aws:iam::<cuenta>:role/<rol-sagemaker> \
  --data data/creditcard.csv
```

Deja un endpoint Serverless Inference: escala solo y no cobra instancia
encendida. Capturas que conviene tomar en el momento:

- la pantalla del endpoint en la consola de SageMaker
- el log del training job en CloudWatch
- las métricas `Invocations` y `ModelLatency` del endpoint

**Borra el endpoint cuando termines de grabar la demostración.** Un endpoint
olvidado consume crédito todos los días.

---

## Configuración

Todo por variables de entorno; ningún secreto en el código ni en la imagen
(EP2 sección 8.2). Valores en `config.py`, plantilla en `.env.example`.

| Variable | Por omisión | Para qué |
|---|---|---|
| `SCORER_BACKEND` | `local` | `local` o `sagemaker` |
| `MODEL_PATH` | `artifacts/model.json` | Artefacto del modelo |
| `SAGEMAKER_ENDPOINT` | — | Nombre del endpoint |
| `REDIS_URL` | `redis://localhost:6379/0` | Feature store |
| `AMQP_URL` | `amqp://guest:guest@localhost:5672/` | Bus de eventos |
| `INTERNAL_BUDGET_MS` | `60` | Corte interno antes del timeout de 80 ms |
| `THRESHOLD_REVIEW` | `0.45` | Score desde el que se revisa |
| `THRESHOLD_REJECT` | `0.80` | Score desde el que se rechaza |
| `RATE_LIMIT_RPS` | `200` | Solicitudes sostenidas por cliente |
| `RATE_LIMIT_BURST` | `400` | Ráfaga tolerada |

---

## Lo que falta

Esto es el esqueleto, no la entrega. Queda por hacer:

- entrenar con el dataset real y reportar precision, recall, F1 y PR-AUC
- desplegar en AWS y capturar las evidencias
- correr la carga contra los dos backends y comparar
- el informe técnico de 10 páginas

`docs/EVIDENCIAS.md` lleva la cuenta punto por punto.
