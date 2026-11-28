# Autoevaluación contra la rúbrica · EP3 ITY1102

Rúbrica oficial de AVA ("Evaluación Parcial 3_ITY1102_40%"): 13 indicadores
que suman 100%. La mitad se evalúa con lo entregado y la otra mitad en la
defensa. La rúbrica no trae los niveles de desempeño; para estimar la nota se
usa la escala habitual de Duoc UC (100, 80, 60, 30 y 0%) con exigencia del 60%
(60% de logro = 4,0).

---

## 1. Lo entregado (50%)

| IE | Peso | Indicador | Logro | Evidencia |
|---|---|---|---|---|
| IE1 | 10% | Modelo de IA funcional en cloud, con endpoints para consumir resultados | 100% | Endpoint Serverless de SageMaker con el modelo versionado y el mismo score que la API; API en EC2 con TLS ([DESPLIEGUE_AWS.md](DESPLIEGUE_AWS.md), capturas 08 a 12) |
| IE2 | 10% | Consumo en una aplicación empresarial con interpretabilidad | 100% | Explicación SHAP en la consola de Fraud Ops (capturas 02 y 04), CP-22 |
| IE4 | 5% | Despliegue en SageMaker verificando escalabilidad y costos | 100% | Carga a 116 tx/s contra el endpoint (RESULTADOS §2.4) y costo proyectado a 1.000.000 tx/día (RESULTADOS §4, punto 7; informe §5) |
| IE5 | 5% | Infraestructura mínima: servidor AWS, modelo, dataset y dashboard | 100% | EC2, modelo con SHA-256, historia simulada y ULB, dashboard en AWS (captura 13) |
| IE7 | 10% | APIs REST en el flujo entre componentes, con interoperabilidad y seguridad | 100% | OpenAPI, TLS 1.3, API key, cuota, secuencia, CP-18, CP-23, `verificar_seguridad.py` |
| IE10 | 10% | Testeo funcional y de rendimiento con métricas | 100% | 25 casos en 110 funciones; p50, p95 y p99 con k6, Locust y sonda propia; métricas del modelo |

**Subtotal estimado: 50 de 50.** La proyección de costo de SageMaker, que
faltaba para IE4, se agregó en esta revisión.

---

## 2. La defensa (50%)

No se puede asegurar desde el repositorio. El material para cada indicador
está en [DEFENSA.md](DEFENSA.md).

| IE | Peso | Indicador | Material de respaldo |
|---|---|---|---|
| IE3 | 10% | Estrategia de despliegue y consumo, y su aporte a la decisión empresarial | D2 medida: 28 ms frente a 660 ms; aprobar, revisar o rechazar con explicación |
| E6 | 10% | Elección de la plataforma AIaaS: rendimiento, costo y flexibilidad | Latencia medida, costo proyectado y `SCORER_BACKEND` |
| IE8 | 5% | Integración, arquitectura de servicios y eficiencia | SHAP asíncrono, un hilo por proceso (62 → 3,5 ms), degradación |
| IE9 | 5% | Sistema funcionando en vivo | Local con `docker compose`, `make demo` para acceso externo, AWS si la sesión está activa |
| IE11 | 5% | Validación, optimización y mejora continua | Partición temporal, comparación con reglas y ULB, CI, hallazgos corregidos |
| IE12 | 5% | Lenguaje formal y técnico | Glosario en DEFENSA §5 |
| IE13 | 10% | Respuestas a las preguntas del docente | 30 preguntas probables con respuesta en DEFENSA §4 |

---

## 3. Nota estimada

| Escenario de la defensa | Logro total | Nota |
|---|---|---|
| Muy buena: todos los indicadores al 100% | 100% | 7,0 |
| Realista: la mayoría al 80%, demo sin fallas | 91% | 6,3 |
| Con problemas: demo de AWS caída y respuestas al 60% | 78,5% | 5,4 |

Los riesgos que más pesan:
- **Demo (IE9).** La IP pública de AWS cambia al reiniciar la instancia; la URL
  del informe puede no responder. Respaldo: versión local y `make demo`.
- **Preguntas (IE13).** Es el indicador individual más pesado de la defensa.
  Conviene que todos los integrantes ensayen la sección 4 de DEFENSA.md.
- **Retroalimentación de la EP2.** El informe solo menciona la EP1; si el
  docente dejó observaciones de la EP2 en AVA, hay que incorporarlas.

---

## 4. Revisión archivo por archivo

Cada archivo del repositorio, el indicador al que aporta y el resultado de la
revisión. "Formal" son los aspectos formales de la pauta (README, Dockerfile,
informe, referencias, nomenclatura).

| Archivo | Indicador | Revisión |
|---|---|---|
| `README.md` | Formal, IE9 | Arranque en un comando, URLs, pruebas, carga, demo externa (`make demo`), decisiones y alcance |
| `Dockerfile` | Formal, IE7 | Multietapa, no root, `OMP_NUM_THREADS=1`, raíz de solo lectura |
| `docker-compose.yml` | IE5, IE7, IE9 | Endurecido, healthchecks, perfiles `carga` y `demo` |
| `deploy/Caddyfile` | IE7 | TLS 1.3, HSTS, 404 para operación, log sin secretos |
| `deploy/*aws*`, `deploy/ec2_user_data.sh`, `deploy/Caddyfile.aws` | IE1, IE5 | Despliegue en EC2 con certificado público y logs en CloudWatch |
| `.env.example` | IE7 | Sin secretos reales; cada variable comentada |
| `.gitignore`, `.gitattributes` | Formal | El modelo aprobado y el PDF de entrega se versionan; la portada local no |
| `.github/workflows/ci.yml` | IE10, IE11 | ruff, pytest con Redis, control del artefacto, humo de la imagen |
| `Makefile` | Formal, IE9 | Atajos para todo el ciclo, incluida la demo |
| `artifacts/*` | IE1, IE10 | Modelo con SHA-256, metadata, métricas, riesgo por comercio y paridad con SageMaker |
| `src/decision_api/*` | IE1, IE7, IE8 | Camino síncrono, autenticación, cuota, degradación, backends local y SageMaker |
| `src/shap_worker/main.py` | IE2, IE8 | Explicación asíncrona con el valor y el aporte de cada feature |
| `src/training/*` | IE1, IE4, IE11 | Simulador, partición temporal, benchmark ULB y despliegue en SageMaker |
| `src/demo/*` | IE9 | Escenarios reproducibles y precarga para la carga |
| `dashboard/app.py` | IE2, IE5, IE9 | Consola de Fraud Ops con explicación legible y vistas directas |
| `load/*` | IE10 | k6, Locust y sonda propia, contrastados entre sí |
| `scripts/*` | IE7, Formal | Verificación de seguridad, generador del informe y paridad con SageMaker |
| `tests/*` | IE10 | 25 casos en 110 funciones; el e2e solo corre con el sistema levantado |
| `docs/RESULTADOS.md` | IE4, IE10, IE11, E6 | Cada cifra con su evidencia, incluido el costo de SageMaker |
| `docs/EVIDENCIAS.md` | Todos | Mapa de la pauta a la evidencia |
| `docs/DESPLIEGUE_AWS.md` | IE1, IE4, IE5 | Pasos reproducibles del despliegue en AWS |
| `docs/DEFENSA.md` | IE3, E6, IE8, IE9, IE11-IE13 | Guion, demo y preguntas probables |
| `docs/diagramas/*` | IE7, IE8 | Secuencia y despliegue en PlantUML |
| `docs/evidencias/*` | IE1, IE4, IE5, IE10 | Capturas locales y de AWS, y salidas crudas fechadas |
| `docs/informe/*` | Formal | Fuente, figuras y PDF de entrega de 10 páginas |
| `data/README.md` | IE5 | Explica los dos datasets |
