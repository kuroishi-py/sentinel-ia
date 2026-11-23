# Mapa de evidencias · EP3 ITY1102

Cada punto de la pauta, qué lo cubre en este repositorio y qué falta capturar.
Se actualiza a medida que avanza el trabajo: lo que está en `[ ]` no existe
todavía.

Leyenda: `[x]` hecho · `[~]` el código está, falta ejecutarlo y capturar · `[ ]` pendiente

---

## Trabajo pendiente

Está como issues, en el milestone
[EP3 - entrega semana 16](https://github.com/kuroishi-py/sentinel-ia/milestone/1).
Tómalos de ahí y asígnate el que vayas a hacer, para que no se pisen dos
personas en lo mismo.

| # | Tarea | Etiquetas |
|---|---|---|
| [1](https://github.com/kuroishi-py/sentinel-ia/issues/1) | Decidir el contrato de features: PCA vs las 11 del diseño | `bloqueante` `modelo` |
| [11](https://github.com/kuroishi-py/sentinel-ia/issues/11) | Confirmar que la pasarela opera en sa-east-1 | `bloqueante` `informe` |
| [2](https://github.com/kuroishi-py/sentinel-ia/issues/2) | Entrenar con el dataset real y reportar métricas | `modelo` `evidencia` |
| [3](https://github.com/kuroishi-py/sentinel-ia/issues/3) | Desplegar el endpoint Serverless de SageMaker | `despliegue` `evidencia` |
| [4](https://github.com/kuroishi-py/sentinel-ia/issues/4) | Comparar p99: modelo en memoria vs endpoint | `testing` `informe` |
| [5](https://github.com/kuroishi-py/sentinel-ia/issues/5) | Tramo asíncrono con RabbitMQ y su evidencia | `despliegue` `evidencia` |
| [6](https://github.com/kuroishi-py/sentinel-ia/issues/6) | Carga a 116 tx/s con p50, p95 y p99 | `testing` `evidencia` |
| [7](https://github.com/kuroishi-py/sentinel-ia/issues/7) | Capturar logs y métricas de CloudWatch | `despliegue` `evidencia` |
| [8](https://github.com/kuroishi-py/sentinel-ia/issues/8) | Dashboard con URL funcional | `despliegue` `evidencia` |
| [9](https://github.com/kuroishi-py/sentinel-ia/issues/9) | Seguridad desplegada: TLS y autenticación | `despliegue` |
| [10](https://github.com/kuroishi-py/sentinel-ia/issues/10) | Informe técnico de 10 páginas | `informe` |
| [12](https://github.com/kuroishi-py/sentinel-ia/issues/12) | Revisar retroalimentación del docente de la EP2 | `informe` `equipo` |
| [13](https://github.com/kuroishi-py/sentinel-ia/issues/13) | Agregar al equipo como colaboradores | `equipo` |

Los dos `bloqueante` van primero: el 1 porque de él dependen el entrenamiento,
el endpoint y la parte de interpretabilidad del informe; el 11 porque si la
pasarela no está en sa-east-1, el presupuesto de latencia del diseño cambia y
hay decisiones que revisar.

---

## 1. Sistema desplegado y funcional

### 1.1 Implementación y despliegue del modelo · IE1, IE2

- `[~]` **Modelo implementado: tipo, framework, entrenamiento, dataset, hiperparámetros**
  → `src/training/train.py`. Los hiperparámetros quedan en `artifacts/metadata.json`.
  **Falta:** entrenar con el dataset real, no con `--synthetic`.
- `[~]` **Despliegue en cloud con endpoints configurados**
  → `src/training/sagemaker_deploy.py`, endpoint Serverless Inference.
  **Falta:** ejecutarlo y capturar la consola de SageMaker.

### 1.2 Plataforma AIaaS e infraestructura · IE4, IE5

- `[~]` **SageMaker: configuración, instancias, auto-scaling, endpoints**
  → `sagemaker_deploy.py`: training job en `ml.m5.xlarge`, endpoint serverless
  con `memory_size_in_mb=2048` y `max_concurrency=20`.
  **Falta:** capturas de la configuración y del endpoint activo.
- `[~]` **Infraestructura mínima operativa**
  Los cuatro elementos que pide la pauta: servidor AWS `[ ]`, modelo ML
  funcional `[~]`, dataset `[ ]`, dashboard `[x]` (`dashboard/app.py`).
- `[~]` **Verificación de escalabilidad: pruebas de carga**
  → `load/locustfile.py`, con los objetivos del caso ya codificados.
  **Falta:** correrla y exportar el CSV.
- `[x]` **Análisis de costos** → Anexo F de la EP2, ya entregado.
- `[ ]` **Evidencias: logs y métricas de CloudWatch**
  Depende de que el sistema esté desplegado.

### 1.3 Integración de servicios · IE7

- `[x]` **APIs REST: arquitectura y endpoints**
  → `POST /v1/score`, `GET /health/live`, `GET /health/ready`, `GET /metrics`.
- `[x]` **Documentación Swagger/OpenAPI**
  → `/docs` y `/openapi.json`, que FastAPI genera. Verificado por CP-13.
  **Falta:** la captura de Swagger UI para el informe.

---

## 2. Informe técnico

### 2.1 Implementación y despliegue del modelo · IE1, IE2

- `[~]` **Integración en aplicación empresarial con interpretabilidad**
  → `dashboard/app.py` muestra la contribución SHAP de cada feature junto al
  caso; `src/shap_worker/main.py` la calcula fuera de la ruta crítica.
- `[ ]` **Capturas del modelo desplegado y funcionando**

### 2.2 Integración de servicios · IE7

- `[x]` **Flujo de comunicación entre componentes distribuidos**
  → `publisher.py` publica en RabbitMQ, `shap_worker` consume. La interfaz de
  administración en `:15672` sirve de captura.
- `[x]` **Seguridad: rate limiting** → `ratelimit.py`, probado por CP-09, CP-10 y CP-17.
  Es el control que faltaba en la EP2.
- `[x]` **Seguridad: plan de autenticación y encriptación TLS** → EP2 sección 8.2.
  **Falta:** la parte que se demuestre desplegada (mTLS o TLS en el ALB).
- `[x]` **Diagrama de secuencia** → Figura 6 de la EP2, se reusa sin cambios.
- `[~]` **Pruebas de interoperabilidad**
  → `tests/test_api.py` cubre la API completa. **Falta:** la prueba extremo a
  extremo con RabbitMQ y el worker corriendo de verdad.

### 2.3 Testing y calidad · IE10

- `[x]` **Mínimo 10 casos de prueba** → 17 casos, CP-01 a CP-17, implementados en
  64 funciones de test. Al 22-11-2026 pasan las 64.

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
  | CP-15 | Idempotencia extremo a extremo | `test_api.py` |
  | CP-16 | Health checks y `/metrics` responden | `test_api.py` |
  | CP-17 | La API devuelve 429 al pasar la cuota | `test_api.py` |

- `[~]` **Latencia p50, p95, p99, throughput y tasa de errores**
  → `telemetry.py` los expone en `/metrics` y Locust los reporta.
  **Falta:** correr la carga y pegar los números.
- `[~]` **Pruebas de carga con JMeter o Locust** → `load/locustfile.py`.
- `[~]` **Métricas del modelo: accuracy, precision, recall, F1**
  → `train.py` las calcula y agrega PR-AUC y la tasa de falsos positivos sobre
  transacciones legítimas, que es la que se compara con el 40% del motor de
  reglas. **Falta:** entrenar con datos reales.
- `[ ]` **Oportunidades de optimización**
  Las de costo están en la EP2 sección 7.5. Las de rendimiento salen después de
  medir.

---

## Aspectos formales

- `[~]` **Repositorio GitHub con README, código y Dockerfile**
  → `README.md`, `Dockerfile`, `docker-compose.yml`. **Falta:** crear el
  repositorio remoto y subirlo.
- `[ ]` **URLs funcionales de dashboard, APIs y modelo**
  Tienen que seguir vivas el día de la revisión.
- `[ ]` **Informe técnico en PDF, 10 páginas**
  El arc42 de la EP2 tiene unas 40. Es un documento nuevo que cita al anterior,
  no un recorte. Conviene decidir la estructura temprano.
- `[x]` **Referencias APA, mínimo 2** → la EP2 trae unas doce.
- `[x]` **Declaración de uso de IA** → plantilla de la EP2, hay que actualizarla
  con lo que se use en esta etapa.
- `[ ]` **Nomenclatura** → `EP3_Equipo7_Martinez_Farenden_PenayLillo_Pavon.pdf`.
  En el Drive hay copias de la EP2 nombradas «EquipoX»: no repetirlo.

---

## Pendientes que la propia EP2 dejó anotados

- `[ ]` **Confirmar que la pasarela opera en sa-east-1.** La EP2 lo dejó como
  riesgo con la mitigación «confirmarlo antes de la EP3» (sección 11.1). Si no
  está en esa región, el presupuesto de latencia completo cambia.
- `[ ]` **Revisar si hubo retroalimentación del docente en la EP2.** La pauta
  pide incorporarla. En la EP1 no hubo observaciones.
