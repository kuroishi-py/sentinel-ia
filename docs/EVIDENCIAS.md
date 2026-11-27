# Mapa de evidencias · EP3 ITY1102

Cada punto de la pauta, qué lo cubre en este repositorio y dónde está la
evidencia. Los números están en [RESULTADOS.md](RESULTADOS.md); los archivos
crudos, en [evidencias/](evidencias/); y la autoevaluación criterio por
criterio, en [RUBRICA.md](RUBRICA.md).

Leyenda: `[x]` hecho y verificado en local · `[aws]` falta la parte que se
despliega en AWS · `[ ]` pendiente del equipo

Estado al 27-11-2026: **todo lo que corre en local está hecho y verificado,
y el informe se genera en PDF de 10 páginas.** Quedan las piezas de AWS
(SageMaker, CloudWatch, URL pública) y la retroalimentación de la EP2.

---

## Decisión del issue #1: contrato de features

**Opción C, dos modelos.** El dataset de la ULB da métricas reales, pero sus
features son PCA: no se pueden servir (la pasarela no manda componentes
principales) y su explicación no sirve para decidir. El modelo de servicio usa
las 11 features del diseño y se entrena con una historia simulada cuyas features
calcula **el mismo código que la API** (`compute_features`). RT2 se cumple por
construcción, y CP-20b lo verifica transacción por transacción.

Detalle y números: [RESULTADOS.md §1](RESULTADOS.md#1-modelo).

---

## 1. Sistema desplegado y funcional

### 1.1 Implementación y despliegue del modelo · IE1, IE2

- `[x]` **Modelo implementado: tipo, framework, entrenamiento, dataset, hiperparámetros**
  - Servicio: `src/training/simulate.py` y `train.py --simulated`, con
    `artifacts/metadata.json` (hiperparámetros, partición, restricciones
    monotónicas) y `artifacts/metrics.json`.
  - Benchmark real: `artifacts/benchmark_ulb/`.
- `[x]` **Endpoint configurado (local)**
  - `POST /v1/score`, por TLS en `https://localhost:8443`.
  - Captura: [01_swagger.png](evidencias/01_swagger.png).
- `[aws]` **Despliegue en cloud**
  - `src/training/sagemaker_deploy.py` sigue listo, pero no se ejecutó.
  - Ojo: hoy entrenaría el modelo de la ULB, no el de servicio.

### 1.2 Plataforma AIaaS e infraestructura · IE4, IE5

- `[aws]` **SageMaker: configuración, instancias, auto-scaling, endpoints.**
- `[x]` **Infraestructura mínima operativa (en local)**
  - Modelo funcional: `artifacts/model.json`, verificado por SHA-256.
  - Dataset: simulado, más el de la ULB.
  - Dashboard: `https://localhost:8444`.
- `[x]` **Verificación de escalabilidad: pruebas de carga**
  - k6 y Locust a 116 tx/s, y capacidad de una tarea de 1 vCPU.
  - [RESULTADOS.md §2](RESULTADOS.md#2-rendimiento) ·
    [carga_k6_pico.txt](evidencias/carga_k6_pico.txt) ·
    [carga_locust_pico_stats.csv](evidencias/carga_locust_pico_stats.csv).
- `[x]` **Análisis de costos.**
  - Anexo F de la EP2, recalculado con la capacidad medida.
  - Tareas de 0,5 vCPU/1 GB: −57% en decision-api (USD 99/mes), con p99 de
    24,2 ms incluso con una zona caída
    ([RESULTADOS.md §4](RESULTADOS.md#4-oportunidades-de-optimización)).
  - Informe, sección 5.
- `[aws]` **Evidencias: logs y métricas de CloudWatch.**
  - En local: logs JSON del borde (sin la API key), la API y el worker en
    [logs_servicios.txt](evidencias/logs_servicios.txt), más
    [metricas_prometheus.txt](evidencias/metricas_prometheus.txt).

### 1.3 Integración de servicios · IE7

- `[x]` **APIs REST: arquitectura y endpoints**
  - `POST /v1/score`, `GET /health/live`, `GET /health/ready`, `GET /metrics`
    y `GET /v1/stats`.
- `[x]` **Documentación Swagger/OpenAPI**
  - `/docs` y `/openapi.json`, con el esquema de seguridad `X-API-Key`.
  - Verificado por CP-13 y CP-18 · [01_swagger.png](evidencias/01_swagger.png).

---

## 2. Informe técnico

### 2.1 Implementación y despliegue del modelo · IE1, IE2

- `[x]` **Integración en aplicación empresarial con interpretabilidad**
  - Cada revisión o rechazo llega por RabbitMQ a shap-worker, que lo explica con
    TreeSHAP.
  - El dashboard lo muestra en lenguaje del analista: "compra en el extranjero,
    dispositivo nuevo, comercio con 9,6% de fraude histórico".
  - Capturas: [02_dashboard_toma_de_cuenta.png](evidencias/02_dashboard_toma_de_cuenta.png),
    [04_dashboard_prueba_de_tarjeta.png](evidencias/04_dashboard_prueba_de_tarjeta.png)
    y [05_dashboard_casos.png](evidencias/05_dashboard_casos.png).
- `[x]` **Capturas del modelo funcionando (en local)**
  - [03_dashboard_calidad_modelo.png](evidencias/03_dashboard_calidad_modelo.png).
  - `[aws]` Faltan las de SageMaker.

### 2.2 Integración de servicios · IE7

- `[x]` **Flujo de comunicación entre componentes distribuidos**
  - Caddy (TLS) → decision-api → Redis (features) → RabbitMQ `decision.v1` →
    shap-worker → caso → dashboard.
  - Capturas: [06_rabbitmq_cola_shap_cases.png](evidencias/06_rabbitmq_cola_shap_cases.png)
    y [07_rabbitmq_exchange_decision_v1.png](evidencias/07_rabbitmq_exchange_decision_v1.png)
    (116 msg/s durante la carga).
- `[x]` **Seguridad: autenticación**
  - API key por cliente (`X-API-Key`, comparación de tiempo constante) y cuota
    también para quien no se autentica.
  - CP-18 · [seguridad_tls_autenticacion.txt](evidencias/seguridad_tls_autenticacion.txt).
- `[x]` **Seguridad: encriptación TLS**
  - Caddy en el borde solo negocia TLS 1.3 (rechaza 1.2), con HSTS.
  - `/metrics` y `/v1/stats` no se ven desde afuera.
  - Verificado por CP-23 y por `scripts/verificar_seguridad.py`.
- `[x]` **Seguridad: rate limiting**
  - Token bucket por cliente autenticado: CP-09, CP-10, CP-17 y CP-18.
  - Bajo carga: 26.357 respuestas 429 sin errores
    ([RESULTADOS.md §2.3](RESULTADOS.md#23-rate-limiting-bajo-carga)).
- `[x]` **Diagrama de secuencia**
  - [ep3_secuencia.puml](diagramas/ep3_secuencia.puml) actualiza la Figura 6
    de la EP2 con lo implementado: autenticación, cuota, degradación y camino
    asíncrono.
  - El despliegue está en [ep3_despliegue_local.puml](diagramas/ep3_despliegue_local.puml).
- `[x]` **Pruebas de interoperabilidad**
  - CP-23, de punta a punta contra el sistema levantado.
  - [pruebas_e2e.txt](evidencias/pruebas_e2e.txt).

### 2.3 Testing y calidad · IE10

- `[x]` **Mínimo 10 casos de prueba**
  - 24 casos (CP-01 a CP-23, más CP-20b) en 101 funciones: 95 unitarias y de
    integración, y 6 de punta a punta.
  - [pruebas_unitarias_integracion.txt](evidencias/pruebas_unitarias_integracion.txt).

  | Caso | Qué verifica | Archivo |
  |---|---|---|
  | CP-01 | Luhn distingue un PAN de un identificador | `test_guards.py` |
  | CP-02 | InputGuard rechaza PAN en cualquier campo | `test_guards.py` |
  | CP-03 | Un `id_tx` repetido devuelve la misma decisión | `test_guards.py` |
  | CP-04 | Un valor corrupto en el store no voltea el servicio | `test_guards.py` |
  | CP-05 | Los umbrales del score están bien aplicados | `test_policy.py` |
  | CP-06 | Las reglas endurecen, nunca ablandan | `test_policy.py` |
  | CP-07 | Una decisión degradada no se aprueba a ciegas | `test_policy.py` |
  | CP-08 | El botón de pánico deja de usar el modelo | `test_policy.py` |
  | CP-09 | La cuota es por cliente y la ráfaga está acotada | `test_ratelimit.py` |
  | CP-10 | El balde se repone con el tiempo, no de golpe | `test_ratelimit.py` |
  | CP-11 | Feature parity: el vector sale en el orden correcto | `test_features_telemetry.py` |
  | CP-12 | p50, p95 y p99 se calculan bien | `test_features_telemetry.py` |
  | CP-13 | La respuesta cumple el contrato de la EP2 | `test_api.py` |
  | CP-14 | Un PAN en caliente da 422, no 500 | `test_api.py` |
  | CP-15 | Idempotencia de punta a punta | `test_api.py` |
  | CP-16 | Health checks y `/metrics` responden | `test_api.py` |
  | CP-17 | La API devuelve 429 al pasar la cuota | `test_api.py` |
  | CP-18 | `/v1/score` exige API key; probar claves tiene cuota | `test_security_resilience.py` |
  | CP-19 | Si Redis falla, se pierde la idempotencia, no el pago | `test_security_resilience.py` |
  | CP-20 | Ventanas de 1 h y 24 h, dispositivo nuevo y paridad Redis/memoria | `test_model_features.py` |
  | CP-20b | El entrenamiento ve las mismas features que la API | `test_model_features.py` |
  | CP-21 | Modelo servido: hash, contrato, comportamiento y monotonía | `test_model_features.py` |
  | CP-22 | La explicación SHAP es exacta y nombra las señales del fraude | `test_model_features.py` |
  | CP-23 | Interoperabilidad: TLS → API → Redis → RabbitMQ → worker → caso | `test_e2e.py` |

- `[x]` **Latencia p50, p95 y p99, throughput y tasa de errores**
  - [RESULTADOS.md §2.1](RESULTADOS.md#21-pico-del-caso-116-txs-durante-3-minutos).
- `[x]` **Pruebas de carga con JMeter o Locust**
  - Locust (`load/locustfile.py`) y k6 (`load/k6_pico.js`).
- `[x]` **Métricas del modelo: accuracy, precision, recall y F1**
  - Del modelo de servicio, del benchmark ULB y del motor de reglas, en
    [RESULTADOS.md §1.2](RESULTADOS.md#12-métricas-sobre-los-14-días-de-prueba-70732-tx-510-fraudes).
- `[x]` **Oportunidades de optimización**
  - [RESULTADOS.md §4](RESULTADOS.md#4-oportunidades-de-optimización).

---

## Aspectos formales

- `[x]` **Repositorio GitHub con README, código y Dockerfile.** Además: CI en
  `.github/workflows/ci.yml` (ruff, pytest con Redis, control del artefacto y
  humo de la imagen).
- `[aws]` **URLs funcionales de dashboard, APIs y modelo.** En local:
  `https://localhost:8443` y `https://localhost:8444`.
- `[x]` **Informe técnico en PDF, 10 páginas**
  - Fuente: [informe/EP3_informe_tecnico.md](informe/EP3_informe_tecnico.md).
  - El PDF lo genera `python scripts/generar_informe.py`. Los PDF no se
    versionan y la portada se completa desde `docs/informe/portada.local.json`,
    porque el repositorio es público.
  - Quedan marcados para completar los datos de AWS, la retroalimentación de la
    EP2 y la fecha.
- `[x]` **Referencias APA, mínimo 2.** En el borrador del informe.
- `[x]` **Declaración de uso de IA.** En el borrador del informe; hay que
  revisarla.
- `[x]` **Nomenclatura:** el script genera `EP3_Equipo7_Martinez_Farenden_PenayLillo_Pavon.pdf`.

---

## Pendientes del equipo

| # | Tarea | Estado |
|---|---|---|
| [1](https://github.com/kuroishi-py/sentinel-ia/issues/1) | Contrato de features | Decidido: opción C, arriba |
| [2](https://github.com/kuroishi-py/sentinel-ia/issues/2) | Entrenar con el dataset real | Hecho (b916db4), ahora en `artifacts/benchmark_ulb/` |
| [3](https://github.com/kuroishi-py/sentinel-ia/issues/3) | Endpoint Serverless de SageMaker | `[aws]` Ver nota abajo |
| [4](https://github.com/kuroishi-py/sentinel-ia/issues/4) | p99 en memoria contra endpoint | `[aws]` El lado en memoria está medido: p99 de 4,5 ms |
| [5](https://github.com/kuroishi-py/sentinel-ia/issues/5) | Tramo asíncrono con RabbitMQ | Hecho: CP-23 y capturas 06 y 07 |
| [6](https://github.com/kuroishi-py/sentinel-ia/issues/6) | Carga a 116 tx/s | Hecho: RESULTADOS §2 |
| [7](https://github.com/kuroishi-py/sentinel-ia/issues/7) | CloudWatch | `[aws]` |
| [8](https://github.com/kuroishi-py/sentinel-ia/issues/8) | Dashboard con URL | Funciona en local; falta la URL pública `[aws]` |
| [9](https://github.com/kuroishi-py/sentinel-ia/issues/9) | TLS y autenticación | Hecho en local (Caddy TLS 1.3 + API key); en AWS: mTLS en el ALB |
| [10](https://github.com/kuroishi-py/sentinel-ia/issues/10) | Informe de 10 páginas | PDF de 10 páginas con `scripts/generar_informe.py`; faltan las partes de AWS y de la EP2 |
| [11](https://github.com/kuroishi-py/sentinel-ia/issues/11) | Pasarela en sa-east-1 | `[ ]` Sigue abierto |
| [12](https://github.com/kuroishi-py/sentinel-ia/issues/12) | Retroalimentación del docente de la EP2 | `[ ]` Revisar en AVA |
| [13](https://github.com/kuroishi-py/sentinel-ia/issues/13) | Colaboradores | `[ ]` |

### Nota para el issue #3 (SageMaker), revisada el 27-11-2026 sin ejecutar nada en AWS

`sagemaker_deploy.py` tal como está no va a funcionar de punta a punta:

1. Por omisión entrena con `data/creditcard.csv` (29 features PCA), pero
   `inference.py` arma el vector con las 11 features del diseño. El endpoint
   recibiría 11 valores para un modelo de 29.
2. El training job ejecuta `src/training/train.py`, que no lee
   `SM_CHANNEL_TRAIN` ni escribe en `SM_MODEL_DIR`: entrenaría datos sintéticos
   y dejaría el modelo donde SageMaker no lo busca.

Camino más corto: desplegar el **modelo de servicio**, que ya está entrenado y
calza con `inference.py`, sin training job:

```bash
tar -czf model.tar.gz -C artifacts model.json
aws s3 cp model.tar.gz s3://<bucket>/sentinel-ia/model.tar.gz
python -m src.training.sagemaker_deploy --role-arn <rol> --skip-training \
       --model-data s3://<bucket>/sentinel-ia/model.tar.gz
```

Riesgo a verificar: el contenedor usa XGBoost `1.7-1` y el modelo se guardó con
2.1.3. Si no carga, usar un `framework_version` más nuevo o reentrenar con 1.7.
