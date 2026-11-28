# Preparación de la defensa · EP3 ITY1102

La mitad de la nota se juega en la presentación. Este documento ordena qué
decir en cada indicador, qué mostrar y qué responder. Las cifras salen de
[RESULTADOS.md](RESULTADOS.md); si se vuelve a medir, hay que actualizar ambos.

| Indicador | Peso | Qué evalúa |
|---|---|---|
| IE3 | 10% | Estrategia de despliegue y consumo de resultados, y su aporte a la decisión empresarial |
| E6 | 10% | Elección de la plataforma AIaaS: rendimiento, costo y flexibilidad |
| IE8 | 5% | Decisiones de integración, arquitectura de servicios e impacto en la eficiencia |
| IE9 | 5% | Sistema funcionando en vivo: modelo prediciendo, dashboard y APIs |
| IE11 | 5% | Estrategias de validación y optimización, y su aporte a la mejora continua |
| IE12 | 5% | Lenguaje formal y técnico |
| IE13 | 10% | Respuestas a las preguntas del docente |

---

## 1. Orden sugerido

Pensado para unos 15 minutos; si el tiempo es otro, se escala cada bloque.

| Bloque | Min | Indicador | Contenido | Qué mostrar |
|---|---|---|---|---|
| Problema | 1 | IE3 | Fraude con tarjeta, 100 ms para decidir, reglas con 9,1% de falsas alarmas | — |
| Arquitectura | 3 | IE8 | Camino síncrono y asíncrono, degradación | Figura 1 y Figura 6 del informe |
| Demo | 4 | IE9, IE2 | Toma de cuenta y prueba de tarjeta, con su explicación | Dashboard, Swagger, RabbitMQ |
| Modelo y validación | 2 | IE11 | Partición temporal, comparación con reglas y con la ULB | Vista "Calidad del modelo" |
| Rendimiento y AIaaS | 3 | E6, IE3 | 4,5 ms frente a 660 ms; costos | Tablas 4.2 y 5 del informe |
| Cierre | 1 | IE11 | Qué se encontró midiendo y qué sigue | Tabla 4.5 del informe |
| Preguntas | — | IE13 | Ver la sección 4 | — |

Conviene que cada integrante presente un bloque y que todos puedan responder
preguntas de cualquiera: IE13 se evalúa sobre el dominio del proyecto completo.

---

## 2. Argumento por indicador

Cada uno en tres frases, con el número que lo sostiene. Dicho así, el
argumento es verificable, no una opinión.

### IE3 · Estrategia de despliegue y aporte a la decisión

1. **El modelo vive dentro de la API, en memoria (decisión D2 de la EP2),** porque
   la pasarela da 100 ms y cualquier salto de red se come el presupuesto.
2. **Lo medimos contra SageMaker con el mismo modelo:** p99 de 28 ms en la API
   frente a 660 ms en el endpoint, con el 21% de los pagos degradados a revisión.
3. **El resultado se consume en tres niveles de decisión:** aprobar sin fricción
   (99,4%), revisar con un analista o rechazar. Cada revisión llega a Fraud Ops
   con su explicación SHAP, y el modelo da 63 falsas alarmas donde las reglas
   daban 6.396.

Evidencia: informe §4.2 y §5; [RESULTADOS.md §2.4](RESULTADOS.md).

### E6 · Elección de la plataforma AIaaS

1. **Rendimiento:** SageMaker Serverless escala a cero y no requiere servidores,
   pero el arranque en frío y el salto de red lo dejan fuera de la ruta del pago
   (p99 de 660 ms). El modelo tarda 2 ms; el resto es la plataforma.
2. **Costo:** a 1.000.000 tx/día, Serverless sumaría entre USD 2,5 y 12 al mes,
   es decir, es barato. Un endpoint de instancia para evitar el arranque en frío
   (2 × `ml.c5.xlarge`) costaría ~USD 298 al mes, cuatro veces la API completa.
3. **Flexibilidad:** `scorer.py` implementa los dos backends con la misma
   interfaz; cambiar es una variable (`SCORER_BACKEND`). SageMaker queda para
   lotes, evaluación en sombra y prueba de versiones nuevas.

Evidencia: informe §5; [RESULTADOS.md §4, punto 7](RESULTADOS.md#4-oportunidades-de-optimización);
capturas 08 y 09.

### IE8 · Integración y arquitectura de servicios

1. **El pago no espera a nada que no necesite.** La API decide con Redis y el
   modelo en memoria; la explicación SHAP se calcula aparte, en shap-worker,
   que consume `decision.v1` de RabbitMQ.
2. **Cada decisión técnica se midió:** un hilo de XGBoost por proceso bajó el p99
   interno de 62 a 3,5 ms; las lecturas de Redis van en una sola pipeline por
   decisión.
3. **Si algo falla, se degrada sin caerse:** Redis caído deja la decisión
   degradada (nunca aprueba a ciegas), RabbitMQ caído deja los eventos en un
   búfer, y sin modelo todo va a revisión.

Evidencia: Figuras 1 y 6 del informe; CP-19 y CP-23; tabla 4.5.

### IE11 · Validación y optimización

1. **Validación honesta del modelo:** partición temporal (se entrena con el
   pasado y se prueba con 14 días que nunca vio), comparación con el motor de
   reglas en los mismos datos y el mismo algoritmo sobre fraude real (ULB).
2. **Calidad continua:** 25 casos de prueba en 110 funciones, CI en cada cambio
   con Redis real y control de que el modelo versionado sea el que sale del
   entrenamiento.
3. **Medir cambió decisiones:** la capacidad real (375 req/s por vCPU) permite
   tareas de 0,5 vCPU y un 57% menos de costo en la API; las pruebas
   encontraron cinco defectos que las unitarias no veían.

Evidencia: informe §4.3 a §4.5.

---

## 3. Demo en vivo (IE9)

### Antes de presentar

1. Levantar todo: `docker compose up -d --build` y esperar a que `docker compose ps`
   muestre `healthy`.
2. Si se presenta desde otro equipo o el proyector no es el PC con Docker:
   `make demo` y abrir la URL del dashboard que imprime.
3. Abrir en pestañas, en este orden:
   - Dashboard, toma de cuenta: `?vista=probar&escenario=ato`
   - Dashboard, prueba de tarjeta: `?vista=probar&escenario=tarjeta`
   - Dashboard, calidad del modelo: `?vista=modelo`
   - Swagger: `/docs`
   - RabbitMQ: <http://localhost:15672> (guest/guest)
4. Si se va a mostrar AWS: iniciar la sesión con tiempo y verificar la URL. La
   IP pública cambia cuando la instancia se reinicia, así que la del informe
   puede no servir.
5. Tener abiertas las capturas de `docs/evidencias/` como respaldo.

### Qué hacer en la demo

1. **Toma de cuenta:** disparar el escenario. Mostrar que la API aprueba las
   compras normales y rechaza la del dispositivo nuevo en el extranjero. A los
   pocos segundos aparece la explicación: país, dispositivo nuevo, comercio
   riesgoso.
2. **Prueba de tarjeta:** la primera sonda se aprueba, la segunda va a revisión
   y desde la tercera se rechaza. Decir: "lo que la delata es la velocidad, no
   el monto".
3. **Swagger:** `POST /v1/score` sin clave devuelve 401; con `X-API-Key`, la
   decisión y su latencia en milisegundos.
4. **RabbitMQ:** la cola `shap.cases` recibiendo las decisiones que se acaban
   de hacer.

### Si algo falla

| Falla | Respuesta |
|---|---|
| AWS no responde | Mostrar la versión local; las cifras de AWS están en las capturas 08 a 14 |
| El dashboard no carga | `docker compose restart dashboard` y mostrar Swagger mientras tanto |
| La explicación no aparece | Revisar `docker compose logs worker`; mostrar la vista de casos con casos anteriores |
| Sin internet | Todo funciona en local sin internet, salvo AWS y los túneles |

---

## 4. Preguntas probables

Respuestas cortas; si el docente pide más, el detalle está en el informe.

### Modelo y datos

**¿Por qué datos simulados y no reales?**
El dataset real disponible (ULB) trae las features como componentes PCA
(`V1`-`V28`): no se pueden calcular en producción, porque la pasarela no envía
componentes principales, y una explicación como "V14 aportó 0,31" no le sirve a
un analista. Por eso hay dos modelos: el de servicio, con las 11 features del
diseño sobre una historia simulada, y el benchmark sobre la ULB, que muestra que
el algoritmo logra lo mismo con fraude real (precision 0,87 y recall 0,84).

**¿Cómo saben que el simulador no hace el problema demasiado fácil?**
Incluye comportamientos legítimos que se parecen al fraude: viajes, cambio de
celular, compras grandes y pasajes de montos chicos. Corregimos un sesgo real:
la primera versión rechazaba pasajes de micro pagados desde un celular nuevo.
Además, el benchmark de la ULB da métricas del mismo orden.

**¿Por qué XGBoost y no una red neuronal?**
Con datos tabulares, el gradient boosting es el estado del arte práctico; predice
en menos de 1 ms en CPU y admite TreeSHAP exacto. Una red neuronal sumaría
latencia y GPU sin ganancia demostrada en este tipo de datos.

**¿Por qué no reportan accuracy?**
La reportamos, pero no sirve: es 0,998, y aprobar todo daría 0,993 porque el
fraude es menos del 1%. Por eso usamos precision, recall, F1 y PR-AUC.

**¿Qué es la partición temporal y por qué importa?**
Entrenamos con los primeros días y probamos con los últimos 14. Una partición
aleatoria mezclaría el futuro en el entrenamiento y daría métricas infladas.

**¿Qué son las restricciones monotónicas?**
Obligan al modelo a que más velocidad, más monto sobre el promedio, un
dispositivo nuevo o un comercio riesgoso nunca bajen el score. Es un control
contra evasión: un atacante no encuentra un "punto dulce" subiendo una variable.

**¿Cómo evitan que el modelo de entrenamiento y el de producción difieran?**
Las features se calculan con la misma función (`compute_features`) en el
entrenamiento y en la API. La prueba CP-20b reproduce cada transacción por la
ruta de la API y exige features idénticas.

**¿Cómo se elige el umbral?**
0,45 para revisión y 0,80 para rechazo. Subir el rechazo a 0,90 evita 18 de 63
falsas alarmas a cambio de dejar pasar 10 de 510 fraudes. Es una decisión de
negocio que depende del costo de cada error.

### Despliegue y AIaaS

**Si SageMaker salió peor, ¿por qué lo usaron?**
Porque la pauta lo pide y porque la decisión había que demostrarla, no
suponerla. El mismo modelo, verificado por SHA-256, dio el mismo score en los dos
lados; la diferencia de latencia es solo de dónde corre. SageMaker sigue siendo
útil para lotes y evaluación en sombra.

**¿Por qué el endpoint tarda 660 ms si el modelo tarda 2 ms?**
Por el salto de red, la sobrecarga de Serverless Inference y el arranque en frío
cuando hay que levantar más concurrencia (máximo 20). La métrica `ModelLatency`
de CloudWatch muestra los 2 ms del modelo; el resto es plataforma.

**¿Por qué no usaron un endpoint de instancia, que no tiene arranque en frío?**
Costaría ~USD 298 al mes con dos instancias para alta disponibilidad, cuatro
veces la API completa, y aun así suma un salto de red. Para un modelo de 2 ms no
se justifica.

**¿Por qué EC2 y no ECS Fargate como en el diseño?**
La red de producción de la EP2 (PrivateLink, NLB, ALB con mTLS) no cabía en el
entorno disponible. Una instancia con el mismo `docker compose` mantiene las
interfaces; Caddy hace de balanceador con TLS 1.3.

**¿Por qué us-east-1 si la pasarela está en Chile?**
Fue una restricción del entorno. Por eso separamos la latencia interna del
servicio de la que ve el cliente: en producción iría en sa-east-1, como dice el
diseño.

**¿Cuánto cuesta?**
La EP2 estimó USD 1.982 al mes para 1.000.000 tx/día, el 4,8% del fraude anual
que se busca contener. Las mediciones bajan la API de USD 173,59 a 74,40 al mes
(−57%) con tareas de 0,5 vCPU. La demo en AWS costó USD 0,03.

**¿Cómo escala?**
Una tarea de 1 vCPU sostiene ~375 solicitudes/s con p99 bajo 30 ms, tres veces
lo supuesto en el diseño. El pico del caso es 116 tx/s, así que tres tareas (una
por zona) sobran, incluso con una zona caída.

### Integración y seguridad

**¿Por qué la API no devuelve la explicación a la pasarela?**
Lo prohíbe el diseño de la EP2: devolver los aportes de cada feature facilita
extraer el modelo. La explicación va solo a Fraud Ops, por el camino asíncrono.

**¿Qué pasa si se cae Redis o RabbitMQ?**
Sin Redis, la decisión sale degradada y nunca se aprueba a ciegas (CP-19). Sin
RabbitMQ, los eventos quedan en un búfer y el pago sigue. Sin modelo, todo va a
revisión.

**¿Cómo se autentica la pasarela?**
En local, con `X-API-Key` sobre TLS 1.3, comparada en tiempo constante. En el
diseño de producción, con mTLS en el ALB. La cuota de solicitudes se aplica por
cliente autenticado.

**¿Qué hacen si la pasarela reintenta la misma transacción?**
La idempotencia por `id_tx` devuelve la misma decisión sin volver a evaluar ni
contar dos veces en las ventanas de velocidad.

**¿Cómo protegen datos sensibles?**
La API rechaza cualquier campo de texto que traiga un número de tarjeta (validación de
Luhn), los secretos van por variables de entorno y el log del borde borra la API
key antes de escribir.

### Pruebas y calidad

**¿Cómo midieron la latencia?**
Con k6 a llegada constante de 116 tx/s durante 3 minutos, contrastado con una
sonda propia y con la medición del propio servidor. Los tres coinciden en 2 a
5 ms de p99.

**¿Por qué Locust da 120 ms si k6 da 4,5 ms?**
Una sonda propia con el mismo patrón de conexiones mide 1,8 ms, así que el
sobrecosto es del cliente de Locust en esta máquina, no del servicio. De paso
encontramos que un cliente sin `TCP_NODELAY` suma 40 ms fijos (Nagle), y lo
dejamos como requisito para la pasarela.

**¿Qué encontraron las pruebas que no esperaban?**
Cinco defectos: un 500 con Redis lento, XGBoost abriendo un hilo por núcleo (p99
de 62 ms), un script de carga que medía otra cosa, el sesgo de los pasajes y la
API key en el log del borde. Todos corregidos.

**¿Cuál es el principal riesgo que queda?**
Un `id_tx` numérico de 13 a 19 dígitos tiene ~10% de probabilidad de pasar la
validación de Luhn y rechazarse como número de tarjeta. El contrato con la
pasarela debe fijar identificadores no numéricos.

### Proceso

**¿Qué hicieron con la retroalimentación de entregas anteriores?**
En la EP1 no hubo observaciones. (Completar con lo que el docente haya dejado en
la EP2.)

**¿Cómo usaron IA generativa?**
Está declarado al final del informe. El diseño viene de la EP1 y la EP2 del
equipo; las cifras se midieron en el sistema y están respaldadas por los
archivos de `docs/evidencias/`.

---

## 5. Lenguaje técnico (IE12)

Términos que conviene usar con precisión:

| Término | Qué significa en este proyecto |
|---|---|
| p50, p95, p99 | Latencia bajo la cual queda el 50, 95 o 99% de las solicitudes. El SLA se fija en p99 |
| Throughput | Solicitudes por segundo atendidas (116 tx/s en el pico) |
| Feature store | Redis con el historial de cada usuario: ventanas de 1 h y 24 h, perfil y dispositivos |
| Training-serving skew | Diferencia entre cómo se calculan las features al entrenar y al servir; se evita con código compartido |
| Inferencia síncrona y asíncrona | La decisión va en el camino del pago; la explicación, fuera de él |
| Degradación controlada | Ante una falla, responder con menos información en vez de caerse |
| Idempotencia | La misma solicitud repetida produce el mismo resultado |
| Arranque en frío | Latencia extra cuando la plataforma levanta capacidad nueva |
| Escala a cero | Sin tráfico, sin instancias ni costo |
| PR-AUC | Área bajo la curva precision-recall; más informativa que ROC con clases desbalanceadas |
| TreeSHAP | Cálculo exacto del aporte de cada feature en modelos de árboles |
