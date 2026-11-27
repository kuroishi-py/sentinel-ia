# Resultados medidos · EP3 ITY1102

Números de la ejecución local del 26 y 27-11-2026, con el sistema completo en
Docker (borde TLS, decision-api, Redis, RabbitMQ, shap-worker y dashboard). Cada
cifra tiene su archivo en [`docs/evidencias/`](evidencias/). La parte de AWS
(SageMaker, CloudWatch) no está cubierta aquí.

**Entorno de medición.** Windows 11 con Docker Desktop, cuya VM tiene **2 vCPU y
4 GB** compartidos con otros contenedores. La API corre como en el Dockerfile:
gunicorn con 2 workers uvicorn. Las cifras son un piso: una tarea Fargate
dedicada no comparte CPU con el generador de carga.

---

## 1. Modelo

### 1.1 Dos modelos, a propósito (issue #1, opción C)

| | Modelo de servicio | Benchmark real |
|---|---|---|
| Para qué | Responde `/v1/score` y se explica en el dashboard | Medir el algoritmo sobre fraude real |
| Datos | Historia simulada: 4.000 usuarios, 300 comercios, 70 días, 315.503 tx, 0,60% fraude | ULB *Credit Card Fraud*: 284.807 tx reales, 0,17% fraude |
| Features | Las 11 del diseño, con nombre | `Amount` y `V1`-`V28` (PCA) |
| Partición | **Temporal**: 21 días de referencia, 35 de entrenamiento (los últimos 7 de validación) y 14 de prueba | Aleatoria estratificada 80/20 |
| Artefacto | `artifacts/` (versionado, SHA-256 `bf46a588…`) | `artifacts/benchmark_ulb/` (solo métricas) |

El benchmark de la ULB tiene métricas reales, pero no se puede servir: la
pasarela no envía componentes principales, y su explicación SHAP ("V14 aportó
0,31") no le sirve a Fraud Ops. El modelo de servicio se entrena con una
historia simulada cuyas features **calcula el mismo código que usa la API**
(`compute_features`, en `src/decision_api/features.py`). La restricción RT2,
*feature parity*, se cumple así por construcción, y la prueba CP-20b la verifica
transacción por transacción.

Patrones simulados:
- **Fraude:** toma de cuenta, prueba de tarjeta y cuentas nuevas. Un 25% de las
  tomas de cuenta es "lenta y baja" y un 20% usa el celular del propio usuario.
- **Legítimos que se parecen al fraude:** viajes, cambio de celular, compras
  grandes, rachas de compras y pasajes de montos chicos.

### 1.2 Métricas sobre los 14 días de prueba (70.732 tx, 510 fraudes)

| Umbral | Precision | Recall | F1 | Falsos positivos | FP sobre legítimas |
|---|---|---|---|---|---|
| 0,45 (revisión) | 0,796 | 0,875 | 0,834 | 114 | 0,16% |
| **0,80 (rechazo)** | **0,871** | **0,831** | **0,851** | 63 | **0,09%** |
| 0,90 | 0,902 | 0,812 | 0,854 | 45 | 0,06% |

- PR-AUC 0,918 · ROC-AUC 0,996.
- Accuracy 0,998: no significa nada con 0,6% de fraude, porque aprobar todo
  daría 0,994.

**Contra un motor de reglas sobre los mismos datos.** Reglas: monto mayor a 3
veces el promedio, extranjero, dispositivo nuevo, 3 o más compras en una hora o
madrugada; alerta si se cumple cualquiera.

| | Precision | Recall | FP sobre legítimas | Alertas falsas |
|---|---|---|---|---|
| Motor de reglas | 0,070 | 0,941 | 9,1% | **93% de las alertas** |
| Modelo (0,80) | 0,871 | 0,831 | 0,09% | 13% de las alertas |

El caso parte de un motor con 40% de falsos positivos. Sobre los mismos datos,
el modelo baja las alertas falsas de 6.396 a 63 (100 veces menos). A cambio
recupera 56 fraudes menos de los que marca el motor. El umbral de revisión
(0,45) recupera parte: recall de 0,875 con 114 falsos positivos.

**Recall por tipo de fraude (umbral 0,45):** toma de cuenta 0,871 · prueba de
tarjeta 0,858 · cuenta nueva 0,965.

**Importancia global (promedio de |SHAP|):** dispositivo nuevo 2,80 · monto
2,74 · riesgo del comercio 2,65 · antigüedad del usuario 2,40 · hora 2,17 ·
compras en la última hora 1,59 · monto acumulado 24 h 1,52 · monto frente al
promedio 1,44 · extranjero 0,68.

**Benchmark ULB (datos reales, umbral 0,80):** precision 0,872 · recall 0,837 ·
F1 0,854 · PR-AUC 0,879 · ROC-AUC 0,978 · 12 falsos positivos en 56.864
legítimas (0,02%). El mismo algoritmo y los mismos hiperparámetros dan
precision y recall del mismo orden sobre fraude real.

### 1.3 Hiperparámetros y controles

- **Algoritmo:** XGBoost 2.1.3, `binary:logistic`.
- **Árboles:** `max_depth` 6, `eta` 0,1, `subsample` 0,8, `colsample_bytree` 0,8,
  `min_child_weight` 5, método `hist`.
- **Rondas:** 299, con parada temprana sobre la validación.
- **Desbalance:** `scale_pos_weight` 166,1.
- **Restricciones monotónicas** (EP2 8.2, control contra evasión AML.T0015):
  más velocidad, más monto sobre el promedio, un dispositivo nuevo, una compra en
  el extranjero o un comercio de más riesgo nunca bajan el score. CP-21 lo
  verifica.
- **SHA-256 del artefacto:** se verifica al cargar; un modelo alterado no se
  carga (CP-21).
- **Reproducibilidad:** con semilla fija, el modelo sale idéntico bit a bit en
  la misma plataforma. Entre Windows y Linux los árboles difieren en el último
  decimal (menos de 0,006 en las métricas), y la CI lo controla con tolerancia.

### 1.4 Un sesgo que se encontró y se corrigió

En la primera versión del simulador, el único origen de montos bajo mil pesos
era la prueba de tarjeta. El modelo aprendió que "monto mínimo + celular nuevo"
es fraude: un pasaje de micro pagado desde un celular nuevo se rechazaba con
score 0,95. Se agregaron montos chicos legítimos (transporte y recargas) y se
reentrenó.

Ahora esa compra se aprueba (0,18). En la prueba de tarjeta, la primera sonda
pasa, la segunda va a revisión y desde la tercera se rechaza: lo que la delata
es la velocidad, no el monto.

---

## 2. Rendimiento

### 2.1 Pico del caso: 116 tx/s durante 3 minutos

| Herramienta | Solicitudes | Throughput | p50 | p95 | p99 | Errores |
|---|---|---|---|---|---|---|
| **k6** (llegada constante) | 20.881 | 116,0 req/s | **1,84 ms** | **2,63 ms** | **4,51 ms** | 0% |
| Locust (`PERFIL=pico`) | 20.600 | 115,0 req/s | 44 ms | 88 ms | 120 ms | 0% |

Latencia interna, del worker que respondió `/v1/stats` al final de la corrida
de k6:

| Tramo | p50 | p95 | p99 |
|---|---|---|---|
| Handler (`latency_ms`) | 0,85 ms | 1,21 ms | 2,04 ms |
| Solicitud completa en el servidor (middleware `ServerTiming`) | 1,56 ms | 2,22 ms | 3,57 ms |

Durante esa corrida hubo 0 decisiones degradadas, 16.704 eventos publicados en
RabbitMQ y 0 descartados. El 99,4% de las decisiones fueron aprobaciones.

**Objetivos de la EP2 (RNF1):** p99 en la pasarela < 100 ms: **cumple** (4,5
ms). p99 interno < 30 ms: **cumple** (3,6 ms). Errores < 1%: **cumple** (0%).

**Por qué Locust mide 44 ms y k6 1,8 ms.** No es el servicio:
- El middleware del servidor mide 1,6 ms por solicitud en la misma corrida.
- Una sonda independiente, en paralelo con la carga de Locust, midió 3,9 ms.
- Locust con un solo usuario mide 3 ms.
- Con 116 conexiones casi ociosas (1 solicitud por segundo cada una), el
  cliente de Locust en Python/gevent suma unos 40 ms por solicitud en esta VM.
  Con `FastHttpUser`, que es lo que la documentación de Locust recomienda para
  medir rendimiento, el efecto se reduce pero no desaparece.
- En el perfil de saturación, con más solicitudes por conexión, Locust mide 2 ms
  de mediana.

Se reporta Locust porque la pauta lo nombra, y k6 porque mide lo que vería la
pasarela.

### 2.2 Capacidad de una tarea de 1 vCPU (`docker update --cpus 1`)

La EP2 (sección 7.4) supuso 120 solicitudes/s por tarea con p99 < 30 ms "a
validar en prueba de carga".

| Tasa (k6, 60 s) | p50 | p95 | p99 | Errores |
|---|---|---|---|---|
| 120 tx/s | 1,98 ms | 3,01 ms | 5,22 ms | 0% |
| 250 tx/s | 1,53 ms | 2,72 ms | 6,98 ms | 0% |
| **375 tx/s** | 1,51 ms | 5,98 ms | **25,4 ms** | 0% |
| 500 tx/s | 1,61 ms | 24,3 ms | 64,0 ms | 0% |
| 800 tx/s | 9,95 ms | 178 ms | 285 ms | 0% (saturada) |

**Una tarea de 1 vCPU sostiene unas 375 solicitudes/s con p99 < 30 ms: tres
veces el supuesto de la EP2.** Con el objetivo de 40 solicitudes/s por tarea, el
mínimo de 3 tareas (una por zona) tiene un margen de 9 veces sobre el pico de
116 tx/s. Ver las oportunidades de optimización (sección 4).

### 2.3 Rate limiting bajo carga

Locust en `PERFIL=capacidad` ofreció 857 req/s durante 1 minuto con una sola
API key:

| Respuesta | Solicitudes | Ritmo |
|---|---|---|
| Decisiones atendidas | 24.920 | — |
| 429 con `Retry-After` | 26.357 | ~440/s |
| Errores del servicio | 0 | — |

Las solicitudes atendidas tuvieron un p99 de 57 ms, y el servicio no se cayó ni
se degradó.

La cuota es de 200/s por cliente **y por proceso**, así que la tarea, con 2
workers, deja pasar hasta unas 400/s. Es la segunda capa: en AWS el límite de
borde va en el ALB o en WAF.

---

## 3. Lo que encontraron las pruebas de carga, y cómo se resolvió

| Hallazgo | Síntoma | Causa | Corrección |
|---|---|---|---|
| Timeouts de Redis devolvían 500 | 5 errores 500 bajo carga, con la decisión ya calculada | `IdempotencyGuard` no capturaba `TimeoutError` | Degrada: se pierde la idempotencia de esa transacción, no el pago. Contador `store_error`. CP-19 |
| `make load` no medía el pico | ~730 req/s en vez de 116; 45% de 429 contados como éxito | Locust con `between(0.05, 0.2)` y `-u 116`; los 429 se marcaban `success()` | Perfiles `pico` (1 req/s por usuario) y `capacidad`; los 429 van en su propia fila |
| p99 interno de 62 ms en Docker | `/health/ready` tardaba segundos | XGBoost abre un hilo OpenMP por núcleo en cada predicción; con 2 workers en 2 vCPU, la espera activa consume la CPU | `nthread=1` en el booster y `OMP_NUM_THREADS=1` en la imagen. p99 interno: 62 → 3,5 ms |
| 51% de revisiones y rechazos en la carga | Tráfico irreal: cada solicitud traía un usuario y un dispositivo nunca vistos | El generador no tenía historia | `src/demo/seed.py` precarga 20.000 perfiles; la carga usa usuarios con historia (2% de dispositivos nuevos, 2% extranjero). Mezcla resultante: 99,4% aprobadas |
| 422 espurios | 68 de 150.000 solicitudes | `id_tx` hexadecimales aleatorios con 13 o más dígitos seguidos que pasan Luhn | Los generadores usan base32. **Riesgo vigente:** un `id_tx` numérico de 13 a 19 dígitos tiene ~10% de probabilidad de rechazarse. El contrato con la pasarela debe fijar identificadores no numéricos |
| Dashboard caído en Docker | Llamaba a `localhost:8000` dentro del contenedor | Ignoraba `API_URL` | Lee `API_URL` y `SENTINEL_API_KEY` |
| Features de velocidad sin efecto | Perfil y dispositivos nunca se escribían; `tx_count_1h` duraba 24 h | El esqueleto incrementaba contadores sin ventanas | Ventanas reales con ZSET, perfil y dispositivos en Redis, en una sola pipeline. CP-20 compara Redis contra memoria |

---

## 4. Oportunidades de optimización

1. **Menos tareas de decision-api.** La capacidad medida (375 req/s por vCPU
   con p99 < 30 ms) permite subir el objetivo de 40 a ~120 solicitudes/s por
   tarea, y la regla de escalado con él. Las 3 tareas mínimas siguen por
   disponibilidad (una por zona), pero el máximo de 12 sobra: con 4 se cubren
   1.000 tx/s. En la estimación de la EP2 (USD 173,59 por 3,5 tareas) eso evita
   pagar escalado que nunca se usa.
2. **Tareas de 0,5 vCPU.** Si una de 1 vCPU sostiene 375 req/s, una de 0,5 vCPU
   sostiene el pico de una zona (39 req/s) con mucho margen. Hay que medirlo con
   el mismo método antes de decidir.
3. **Rate limiting compartido.** El token bucket es por proceso. Si se quiere un
   límite exacto por cliente en toda la flota, debe vivir en Redis o en el borde
   (WAF).
4. **Reparto entre workers.** Con conexiones persistentes, un worker atendió
   16.704 de 20.881 solicitudes. Sin consecuencias a 116 tx/s, pero cerca de la
   saturación conviene `--max-requests` en gunicorn, o que el ALB abra más
   conexiones.
5. **SHAP por lotes.** shap-worker explica de a un evento. Con un pico de
   revisiones, procesar en lotes de 50 reduce la CPU por caso.
6. **Umbral de rechazo.** Entre 0,80 y 0,90, F1 casi no cambia (0,851 contra
   0,854) y se evitan 18 falsos positivos de cada 63, a cambio de 10 fraudes de
   cada 510. Es una decisión de negocio que conviene tomar con el costo de cada
   error.

---

## 5. Cómo reproducir

```bash
docker compose up -d --build
docker compose exec api python -m src.demo.seed --users 20000
docker compose --profile carga run --rm k6                       # 116 tx/s, 3 min
PERFIL=pico docker compose --profile carga run --rm locust       # la pauta pide Locust o JMeter
PERFIL=capacidad DURACION=1m docker compose --profile carga run --rm locust

# capacidad de una tarea de 1 vCPU
RATE_LIMIT_RPS=100000 RATE_LIMIT_BURST=100000 docker compose up -d api
docker update --cpus 1 sentinel-api
TASA=375 DURACION=60s docker compose --profile carga run --rm k6
docker compose up -d --force-recreate api                        # vuelve a la normalidad
```
