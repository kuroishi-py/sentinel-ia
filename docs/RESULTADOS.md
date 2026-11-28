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

**Por qué Locust mide 44 ms y k6 1,8 ms.** No es el servicio. Tres clientes
independientes coinciden en 2 a 5 ms, y solo Locust se aparta:

| Cliente, contra la misma tarea | p50 | p99 |
|---|---|---|
| Middleware del servidor (solicitud completa) | 1,6 ms | 3,6 ms |
| k6, 116 tx/s de llegada constante | 1,8 ms | 4,5 ms |
| Sonda propia: 116 conexiones TCP, 1 req/s cada una (`load/sonda_conexiones.py`) | 1,8 ms | 5,3 ms |
| Locust con 1 usuario | 3 ms | — |
| Locust con 116 usuarios (1 req/s cada uno) | 44 ms | 120 ms |

La sonda reproduce el mismo patrón de conexiones que Locust y da 1,8 ms, así
que el servidor no se degrada con muchas conexiones ociosas: el sobrecosto es
del cliente de Locust (Python sobre gevent) en esta VM. Locust se reporta porque
la pauta lo nombra; k6 y la sonda, porque miden lo que vería la pasarela
([sonda_conexiones.txt](evidencias/sonda_conexiones.txt)).

**Un hallazgo para el contrato con la pasarela.** Un cliente que envía
cabeceras y cuerpo del POST en dos escrituras **sin `TCP_NODELAY`** suma 40 ms
fijos por solicitud:

| Mismo POST, misma conexión | p50 |
|---|---|
| Una escritura | 3,0 ms |
| Dos escrituras, con `TCP_NODELAY` | 3,4 ms |
| Dos escrituras, sin `TCP_NODELAY` | **44,7 ms** |

Es el algoritmo de Nagle del cliente, que espera el ACK que Linux retarda 40
ms. El log del borde registra justo esos ~45 ms para un cliente Python desde
Windows. El servidor ya usa `TCP_NODELAY` (verificado en uvicorn/uvloop). La
pasarela debe usar un cliente HTTP con `TCP_NODELAY`, como hacen por omisión Go,
Netty y OkHttp. Si no, el 40% del presupuesto de 100 ms se pierde antes de
llegar al servicio.

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
116 tx/s.

**Y una de 0,5 vCPU** (`docker update --cpus 0.5`), que es lo que propone la
sección 4:

| Tasa (k6, 60 s) | Qué representa | p50 | p99 | Errores |
|---|---|---|---|---|
| 39 tx/s | El pico repartido en 3 zonas | 2,35 ms | **22,5 ms** | 0% |
| 58 tx/s | El pico con una zona caída (2 tareas) | 2,18 ms | **24,2 ms** | 0% |
| 116 tx/s | Todo el pico en una sola tarea | 1,87 ms | 39,9 ms | 0% |
| 200 tx/s | — | 1,59 ms | 60,8 ms | 0% |

Con media CPU, la latencia de cola sube incluso con poca carga (22 ms de p99 a
39 tx/s): es el *throttling* de CPU del contenedor. Aun así, en los dos
escenarios del diseño (normal y con una zona caída) se cumple el p99 interno de
30 ms, con un margen de ~20%.

**Memoria medida:** 229 MB de RSS por worker y 284 MiB la tarea completa bajo
carga. La EP2 supuso 0,45 GB por proceso, así que una tarea de 1 GB alcanza.

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

### 2.4 En AWS: modelo en la API frente al endpoint de SageMaker (decisión D2)

Medido el 27-11-2026 en AWS Academy (`us-east-1`): instancia EC2 `t3.medium`
con el mismo `docker compose`, y endpoint Serverless Inference
`sentinel-ia-20261127-1928` (2.048 MB, concurrencia máxima 20) con el mismo
artefacto versionado (`v1.0.0-sim`, SHA-256 `bf46a588…`). k6 corre dentro de la
instancia contra la API, 116 tx/s de llegada constante durante 2 minutos. Lo
único que cambia entre las dos filas es `SCORER_BACKEND`. Pasos en
[DESPLIEGUE_AWS.md](DESPLIEGUE_AWS.md).

| Backend | Solicitudes | p50 | p95 | p99 | Máx. | Errores HTTP | Degradadas | p99 < 100 ms |
|---|---|---|---|---|---|---|---|---|
| Modelo en la API (D2) | 13.921 (116,0/s) | 3,16 ms | 8,42 ms | 28,19 ms | 84,53 ms | 0,00% | 0 | Cumple |
| Endpoint de SageMaker | 13.921 (116,0/s) | 42,74 ms | 255,08 ms | 659,72 ms | 909,27 ms | 0,00% | 2.946 (21%) | No cumple |

**La decisión D2 de la EP2 queda confirmada con datos.** El endpoint suma un
salto de red y la sobrecarga de Serverless Inference: el p99 pasa de 28 ms a
660 ms. Ninguna solicitud falla, porque la API corta a los 60 ms internos y
responde degradado; pero el 21% de los pagos queda en revisión manual, lo que
anula el objetivo de bajar los falsos positivos. El endpoint sirve para
entrenamiento, pruebas en sombra y procesos por lotes, no para la ruta del pago.

Paridad del modelo (`sagemaker_deploy.py --check`, desde la terminal del
laboratorio): los tres escenarios de `artifacts/sagemaker_parity.json` dan el
mismo score en la API y en el endpoint (diferencia 0). Latencia por invocación
desde esa terminal: 448 ms la primera (arranque en frío), 105 y 217 ms las
siguientes.

---

## 3. Lo que encontraron las pruebas, y cómo se resolvió

| Hallazgo | Síntoma | Causa | Corrección |
|---|---|---|---|
| Timeouts de Redis devolvían 500 | 5 errores 500 bajo carga, con la decisión ya calculada | `IdempotencyGuard` no capturaba `TimeoutError` | Degrada: se pierde la idempotencia de esa transacción, no el pago. Contador `store_error`. CP-19 |
| `make load` no medía el pico | ~730 req/s en vez de 116; 45% de 429 contados como éxito | Locust con `between(0.05, 0.2)` y `-u 116`; los 429 se marcaban `success()` | Perfiles `pico` (1 req/s por usuario) y `capacidad`; los 429 van en su propia fila |
| p99 interno de 62 ms en Docker | `/health/ready` tardaba segundos | XGBoost abre un hilo OpenMP por núcleo en cada predicción; con 2 workers en 2 vCPU, la espera activa consume la CPU | `nthread=1` en el booster y `OMP_NUM_THREADS=1` en la imagen. p99 interno: 62 → 3,5 ms |
| 51% de revisiones y rechazos en la carga | Tráfico irreal: cada solicitud traía un usuario y un dispositivo nunca vistos | El generador no tenía historia | `src/demo/seed.py` precarga 20.000 perfiles; la carga usa usuarios con historia (2% de dispositivos nuevos, 2% extranjero). Mezcla resultante: 99,4% aprobadas |
| 422 espurios | 68 de 150.000 solicitudes | `id_tx` hexadecimales aleatorios con 13 o más dígitos seguidos que pasan Luhn | Los generadores usan base32. **Riesgo vigente:** un `id_tx` numérico de 13 a 19 dígitos tiene ~10% de probabilidad de rechazarse. El contrato con la pasarela debe fijar identificadores no numéricos |
| Dashboard caído en Docker | Llamaba a `localhost:8000` dentro del contenedor | Ignoraba `API_URL` | Lee `API_URL` y `SENTINEL_API_KEY` |
| API key en los logs del borde | El log de acceso de Caddy guardaba la cabecera `X-Api-Key` en claro | El formato JSON registra todas las cabeceras | Filtro `request>headers>X-Api-Key delete` en el Caddyfile; verificado sin apariciones de la clave ([logs_servicios.txt](evidencias/logs_servicios.txt)) |
| Features de velocidad sin efecto | Perfil y dispositivos nunca se escribían; `tx_count_1h` duraba 24 h | El esqueleto incrementaba contadores sin ventanas | Ventanas reales con ZSET, perfil y dispositivos en Redis, en una sola pipeline. CP-20 compara Redis contra memoria |

---

## 4. Oportunidades de optimización

1. **Dimensionar decision-api con la capacidad medida.** Costo según el Anexo F
   (AWS Pricing Calculator, São Paulo, ARM): una tarea de 1 vCPU y 2 GB cuesta
   USD 49,60 al mes (173,59 / 3,5), y una de 0,5 vCPU y 1 GB, USD 24,80. Esta
   última es la tarifa que el mismo anexo usa para case-service y shap-worker.

   | Opción | Tareas | USD/mes | Ahorro/mes | Respaldo |
   |---|---|---|---|---|
   | EP2: 1 vCPU/2 GB, 3,5 en promedio | 3,5 | 173,59 | — | Supuesto de 120 req/s por tarea |
   | 1 vCPU/2 GB, sin escalar sobre el mínimo | 3 | 148,79 | 24,80 | Medido: 375 req/s por tarea; el pico nunca obliga a escalar |
   | **0,5 vCPU/1 GB, 3 tareas** | 3 | **74,40** | **99,19 (57%)** | Medido: p99 de 22,5 ms normal y 24,2 ms con una zona caída; 284 MiB de memoria |

   La tercera opción ahorra USD 1.190 al año, el 5% de la estimación total de
   la EP2 (USD 23.784). Antes de adoptarla hay que repetir la medición en
   Fargate ARM: el margen frente a los 30 ms es de ~20%, y la CPU de Docker
   Desktop no es la de Graviton.
2. **Ajustar la regla de escalado.** El objetivo de 40 solicitudes/s por tarea
   es tres veces más conservador que lo medido. Subirlo a ~120 (1 vCPU) o ~40
   (0,5 vCPU), y bajar el máximo de 12 tareas, evita pagar escalado que nunca se
   usa.
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

7. **Dónde servir el modelo: SageMaker frente a la API, con costo.** A
   1.000.000 tx/día son 30 millones de invocaciones al mes. Precios de la
   página oficial de SageMaker (<https://aws.amazon.com/sagemaker/ai/pricing/>,
   consultada el 27-11-2026): Serverless de 2.048 MB a USD 0,00004 por segundo
   de cómputo y USD 0,016 por GB de datos procesados.

   | Opción | Cálculo | USD/mes adicional | p99 medido (§2.4) |
   |---|---|---|---|
   | Modelo en la API (D2) | Ya incluido en las tareas de decision-api | 0 | 28 ms |
   | Serverless, 2 ms por invocación (`ModelLatency` medido) | 30 M × 0,002 s × 0,00004 + 3,6 GB × 0,016 | 2,46 | 660 ms, 21% degradadas |
   | Serverless, 10 ms por invocación (cota conservadora) | 30 M × 0,010 s × 0,00004 + 3,6 GB × 0,016 | 12,06 | — |
   | Instancia, 2 × `ml.c5.xlarge` | 2 × 730 h × 0,204 | 297,84 | No medido |

   - Los ~120 bytes por invocación son las 11 features en CSV más el score.
   - El precio de `ml.c5.xlarge` es el de los ejemplos de la misma página;
     conviene confirmarlo en el AWS Pricing Calculator para la región final.
   - En todos los casos las tareas de decision-api siguen siendo necesarias:
     calculan las features y aplican la política. SageMaker se suma, no
     reemplaza.

   Conclusión: Serverless es barato, pero su latencia (arranque en frío, salto
   de red, concurrencia máxima de 20) deja pagos en revisión; un endpoint de
   instancia evitaría el arranque en frío, pero cuesta cuatro veces la API
   completa para un modelo que tarda 2 ms. La decisión D2 se sostiene en
   rendimiento y en costo. SageMaker conviene para lotes, evaluación en sombra
   y prueba de versiones nuevas, donde escalar a cero sí ahorra.

---

## 5. Cómo reproducir

```bash
docker compose up -d --build
docker compose exec api python -m src.demo.seed --users 20000
docker compose --profile carga run --rm k6                       # 116 tx/s, 3 min
PERFIL=pico docker compose --profile carga run --rm locust       # la pauta pide Locust o JMeter
PERFIL=capacidad DURACION=1m docker compose --profile carga run --rm locust

# capacidad de una tarea de 1 vCPU (o 0.5)
RATE_LIMIT_RPS=100000 RATE_LIMIT_BURST=100000 docker compose up -d api
docker update --cpus 1 sentinel-api
TASA=375 DURACION=60s docker compose --profile carga run --rm k6
docker compose up -d --force-recreate api                        # vuelve a la normalidad

# latencia sin el cliente de Locust: 116 conexiones, 1 req/s cada una
docker compose exec -T dashboard python - < load/sonda_conexiones.py
```
