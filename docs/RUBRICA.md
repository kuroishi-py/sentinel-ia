# Autoevaluación contra la rúbrica · EP3 ITY1102

**La rúbrica oficial no estaba disponible.** El documento de la pauta
(`EP3_ITY1102_Estudiante.docx`) trae los requisitos y sus indicadores, pero no
la tabla de niveles. Esta rúbrica se reconstruyó así:
- un criterio por cada requisito de la pauta, con su indicador (IE);
- la escala de desempeño habitual de Duoc UC: 100% muy buen desempeño, 80%
  bueno, 60% aceptable, 30% incipiente y 0% no logrado.

Cuando el equipo tenga la rúbrica de AVA, hay que repetir esta revisión contra
ella.

El alcance acordado excluye AWS. Los criterios que dependen de AWS se marcan
**AWS** y no se evalúan aquí.

---

## 1. Criterios y nivel alcanzado

"Antes" es el estado del repositorio al iniciar la revisión (commit `e2e7e85`),
y "Ahora", el de esta versión.

| # | Criterio (pauta) | IE | Antes | Ahora | Evidencia |
|---|---|---|---|---|---|
| 1 | Modelo implementado: tipo, framework, entrenamiento, datasets, hiperparámetros | IE1, IE2 | 100% | 100% | `artifacts/metadata.json`, RESULTADOS §1, informe §2.1-2.2 |
| 2 | Despliegue con endpoints configurados | IE1, IE2 | 80% | 100% local · AWS | API detrás de TLS con health checks y SHA-256; informe §2.3 |
| 3 | Plataforma AIaaS: SageMaker (instancias, auto-scaling, endpoints) | IE4, IE5 | AWS | AWS | Nota del issue #3 en EVIDENCIAS |
| 4 | Infraestructura mínima: servidor, modelo, dataset, dashboard | IE4, IE5 | 100% | 100% | `docker compose up`; capturas 02-05 |
| 5 | Escalabilidad: pruebas de carga | IE4, IE5 | 80% | 100% | k6, Locust, sonda propia, capacidad con 1 y 0,5 vCPU (RESULTADOS §2) |
| 6 | Análisis de costos | IE4, IE5 | 60% | 100% | Recalculado con el Anexo F y la capacidad medida (RESULTADOS §4, informe §5) |
| 7 | Evidencias: logs y métricas | IE4, IE5 | 60% | 100% local · AWS | `logs_servicios.txt`, `metricas_prometheus.txt`; CloudWatch es AWS |
| 8 | API REST: arquitectura, endpoints, Swagger/OpenAPI | IE7 | 100% | 100% | `/docs`, CP-13, CP-18, captura 01 |
| 9 | Integración empresarial con interpretabilidad | IE1, IE2 | 100% | 100% | Explicación SHAP legible; capturas 02 y 04; CP-22 |
| 10 | Capturas del modelo desplegado y funcionando | IE1, IE2 | 80% | 100% local · AWS | Capturas sin menú de desarrollo; las de SageMaker son AWS |
| 11 | Flujo de comunicación entre componentes distribuidos | IE7 | 80% | 100% | Diagrama de despliegue y captura de RabbitMQ con 116 msg/s |
| 12 | Seguridad: autenticación, TLS, rate limiting | IE7 | 80% | 100% | Raíz de solo lectura aplicada; secreto fuera de los logs; `verificar_seguridad.py` |
| 13 | Diagrama de secuencia | IE7 | 30% | 100% | `docs/diagramas/ep3_secuencia.puml` (antes solo se citaba el de la EP2) |
| 14 | Pruebas de interoperabilidad | IE7 | 100% | 100% | CP-23, `pruebas_e2e.txt` |
| 15 | Mínimo 10 casos de prueba; unitarias e integración | IE10 | 100% | 100% | 24 casos, 101 funciones, CI |
| 16 | Latencia p50/p95/p99, throughput, tasa de errores | IE10 | 80% | 100% | Locust ahora se explica con mediciones, no con una suposición |
| 17 | Pruebas de carga (JMeter/Locust) | IE10 | 100% | 100% | `load/locustfile.py`, CSV de Locust |
| 18 | Métricas del modelo: accuracy, precision, recall, F1 | IE10 | 100% | 100% | Servicio, benchmark ULB y motor de reglas |
| 19 | Oportunidades de optimización | IE10 | 80% | 100% | Cada propuesta medida y costeada (0,5 vCPU: −57%) |
| 20 | Informe técnico en PDF de 10 páginas | Formal | 30% | 90% | `scripts/generar_informe.py`, 10 páginas; faltan los datos de AWS y la retroalimentación de la EP2 |
| 21 | Repositorio con README, código y Dockerfile | Formal | 100% | 100% | README, Dockerfile endurecido, CI |
| 22 | Sistema con URLs funcionales | Formal | 80% | 80% · AWS | Funciona en `https://localhost:8443` y `:8444`; la URL pública es AWS |
| 23 | Referencias APA (mínimo 2) | Formal | 100% | 100% | 6 referencias con DOI |
| 24 | Declaración de uso de IA | Formal | 80% | 100% | Informe, sección final |
| 25 | Nomenclatura `EP3_Equipo[N]_Apellidos.pdf` | Formal | 0% | 100% | `docs/informe/EP3_Equipo7_Martinez_Farenden_PenayLillo_Pavon.pdf` |
| 26 | Incorporar la retroalimentación de entregas anteriores | Transversal | 30% | 30% | Depende de lo que el docente dejó en AVA sobre la EP2 (issue #12) |

**Sin contar AWS**, los criterios evaluables quedan en el nivel máximo, salvo
dos que dependen de información que solo tiene el equipo:
- **20, informe:** con la portada local quedan 4 `[COMPLETAR]`. Dos son de
  AWS: el endpoint de SageMaker con sus capturas, y su costo. Los otros dos son
  la retroalimentación de la EP2 y la fecha de entrega. Sin la portada local
  (versión pública) son 6, porque también faltan los integrantes y el docente.
- **26, retroalimentación:** hay que revisar AVA.

---

## 2. Qué cambió en esta iteración

| Brecha | Criterio | Qué se hizo | Verificación |
|---|---|---|---|
| El informe citaba el diagrama de secuencia de la EP2 sin actualizarlo | 13 | Secuencia nueva en PlantUML (convención C2 del equipo) con autenticación, cuota, degradación y camino asíncrono | Renderizada con `plantuml/plantuml` |
| No había diagrama del despliegue implementado | 11 | `ep3_despliegue_local.puml` | Figura 1 del informe |
| No había logs como evidencia | 7 | Log de acceso JSON en el borde y extracto de los tres servicios | `logs_servicios.txt` |
| **El log del borde guardaba la API key en claro** | 12 | Filtro `request>headers>X-Api-Key delete` | Cero apariciones de la clave después del cambio |
| El Dockerfile decía "raíz de solo lectura" y el compose no lo aplicaba | 12 | `read_only`, `tmpfs` y `no-new-privileges` en api, worker y dashboard | `ReadonlyRootfs=true`; e2e en verde |
| Los costos solo remitían a la EP2 | 6, 19 | Capacidad con 0,5 vCPU (p99 de 22,5 y 24,2 ms en los escenarios del diseño) y memoria medida (284 MiB); costo recalculado con el Anexo F | RESULTADOS §2.2 y §4 |
| La diferencia de Locust se explicaba con una suposición | 16 | Sonda propia de 116 conexiones (1,8 ms) y experimento de Nagle (40 ms sin `TCP_NODELAY`) | `sonda_conexiones.txt` |
| El informe era un borrador sin PDF ni conclusiones | 20, 25 | PDF de 10 páginas, generado de forma reproducible, con conclusiones y figuras recortadas | `python scripts/generar_informe.py` |
| Las capturas mostraban el botón "Deploy" de Streamlit, y la tabla, "None" | 10 | `toolbarMode=viewer`; celdas vacías como "—"; nota de accuracy con los datos reales (0,72% de fraude) | Capturas 02-05 nuevas |

---

## 3. Revisión archivo por archivo

Cada archivo del repositorio, el criterio al que aporta y el resultado de la
revisión.

| Archivo | Criterio | Revisión |
|---|---|---|
| `README.md` | 21 | Arranque en un comando, URLs, pruebas, carga, decisiones y alcance. Al día |
| `Dockerfile` | 21, 12 | Multietapa, no root, `OMP_NUM_THREADS=1`, `--worker-tmp-dir /tmp` para la raíz de solo lectura, versión 1.0.0. Corregido el comentario que prometía algo que no aplicaba |
| `docker-compose.yml` | 4, 11, 12 | Endurecido; healthchecks correctos; puertos de desarrollo solo en 127.0.0.1; servicios de carga en el perfil `carga` |
| `deploy/Caddyfile` | 12 | TLS 1.3, HSTS, 404 para operación, log sin secretos |
| `.env.example` | 12 | Sin secretos reales; cada variable comentada |
| `.gitignore`, `.gitattributes` | 21 | El modelo aprobado se versiona y sus bytes no se tocan; se versiona solo el PDF de entrega, no la portada local |
| `.github/workflows/ci.yml` | 15, 21 | ruff, pytest con Redis, control del artefacto, humo de la imagen |
| `Makefile` | 21 | Atajos para todo el ciclo |
| `pyproject.toml`, `requirements*.txt` | 21 | Versiones fijas; marcador `e2e`; `markdown` para el informe |
| `artifacts/*` | 1, 18 | Modelo con SHA-256, metadata (hiperparámetros, partición, monotonía), métricas y riesgo por comercio |
| `artifacts/benchmark_ulb/*` | 1, 18 | Métricas reales de la ULB, sin cambios |
| `src/decision_api/main.py` | 8, 12 | Orden del camino síncrono, autenticación, cuota, ServerTiming, `/v1/stats` |
| `src/decision_api/features.py` | 1, 9 | Función pura compartida con el entrenamiento; Redis en una pipeline |
| `src/decision_api/guards.py` | 12 | PAN por Luhn; idempotencia tolerante a fallas |
| `src/decision_api/scorer.py` | 2 | SHA-256, warm-up, un hilo por proceso, backend SageMaker |
| `src/decision_api/policy.py`, `ratelimit.py`, `schemas.py` | 8, 12 | Sin cambios; ya cubiertos por CP-05 a CP-10 |
| `src/decision_api/publisher.py` | 11 | Sin cambios; 0 eventos descartados bajo carga |
| `src/decision_api/telemetry.py`, `config.py` | 16 | Contadores nuevos y percentiles del servidor |
| `src/shap_worker/main.py` | 9 | Guarda los valores de las features junto a su aporte |
| `src/training/simulate.py`, `train.py` | 1 | Partición temporal, controles anti-fuga, métricas por escenario y línea base de reglas |
| `src/training/download_dataset.py` | 1 | Sin cambios |
| `src/training/inference.py`, `sagemaker_deploy.py` | 3 | AWS: no se ejecutaron. Los problemas que impedirían el despliegue están documentados en EVIDENCIAS |
| `src/demo/scenarios.py`, `seed.py` | 5, 9 | Escenarios reproducibles y precarga para una carga realista |
| `dashboard/app.py` | 4, 9 | Explicación legible, vistas directas, sin "None" ni datos inventados |
| `load/locustfile.py`, `k6_pico.js`, `sonda_conexiones.py` | 5, 16, 17 | Tres generadores que se contrastan entre sí |
| `scripts/verificar_seguridad.py` | 12 | 12 verificaciones del borde, todas OK |
| `scripts/generar_informe.py` | 20 | Del markdown al PDF con número de página; cuenta páginas y `[COMPLETAR]` |
| `tests/*` | 15 | 24 casos; el e2e solo corre con el sistema levantado |
| `docs/RESULTADOS.md` | 5, 6, 16, 18, 19 | Cada cifra con su archivo de evidencia |
| `docs/EVIDENCIAS.md` | todos | Mapa de la pauta a la evidencia |
| `docs/diagramas/*` | 11, 13 | Fuente `.puml` y PNG |
| `docs/evidencias/*` | 7, 10, 14-17 | Capturas y salidas crudas fechadas |
| `docs/informe/*` | 20, 23-25 | Fuente del informe, figuras y PDF de entrega |
| `data/README.md` | 1 | Explica los dos datasets |
