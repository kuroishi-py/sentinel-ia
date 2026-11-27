# Despliegue en AWS · EP3

Cómo se lleva SENTINEL-IA a AWS Academy (Learner Lab) para cubrir lo que la
pauta pide en la nube: el modelo en un endpoint de SageMaker, el sistema con
URLs públicas, y logs y métricas en CloudWatch. Todo se ejecuta desde **AWS
CloudShell**, sin instalar nada en el computador.

| Pieza | Qué queda | Punto de la pauta |
|---|---|---|
| Endpoint de SageMaker | Serverless Inference con el mismo modelo versionado de la API | 1.1, 1.2 |
| Instancia EC2 | `docker compose` completo detrás de Caddy, con certificado público | 1.2, 1.3 |
| CloudWatch | Grupos `/sentinel-ia/*` y métricas del endpoint y de la instancia | 1.2 |

## Decisiones

- **No se reentrena en AWS.** Se publica el mismo `artifacts/model.json` que
  sirve la API, verificado por SHA-256. Así los dos backends de `scorer.py`
  son el mismo modelo y la comparación de latencia mide solo dónde corre
  (decisión D2 de la EP2). `src/training/sagemaker_deploy.py` lo comprueba:
  manda al endpoint las transacciones de `artifacts/sagemaker_parity.json` y
  compara con el score de la API.
- **Región `us-east-1`.** Learner Lab no habilita `sa-east-1`. Es una
  desviación del diseño: con la pasarela en Santiago se sumaría la latencia
  de red hasta Virginia. Por eso los resultados separan la latencia interna
  (`latency_ms`, `/v1/stats`) de la que mide el cliente.
- **EC2 en vez de ECS Fargate.** La red de producción de la EP2 (PrivateLink,
  NLB, ALB con mTLS) no cabe en Learner Lab. Una instancia con el mismo
  `docker compose` mantiene las interfaces; Caddy hace de ALB con TLS 1.3 y
  certificados de Let's Encrypt sobre `sslip.io`.

## Pasos

### 1. Abrir CloudShell

Learner Lab → *Start Lab* → *AWS* (consola) → ícono de CloudShell. Región
`us-east-1`.

### 2. Traer el repositorio

```bash
git clone https://github.com/kuroishi-py/sentinel-ia.git
cd sentinel-ia
```

### 3. Endpoint de SageMaker

```bash
pip3 install --user -q "sagemaker<3"
python3 src/training/sagemaker_deploy.py --region us-east-1
```

Toma de 3 a 8 minutos. Termina con la tabla de paridad (score de la API frente
al del endpoint, que deben coincidir) y el nombre del endpoint. Si Learner Lab
no permite endpoints serverless, usar uno de instancia:
`--instance-type ml.m5.large`.

### 4. Instancia EC2

```bash
bash deploy/aws_ec2.sh
```

Crea el grupo de seguridad (80 y 443 abiertos, 22 solo para CloudShell), la
llave SSH, una IP elástica y la instancia `t3.medium`. La instancia se
configura sola en unos 10 minutos con `deploy/ec2_user_data.sh`. Las URLs, el
usuario y la contraseña del dashboard y la API key quedan en
`~/sentinel-ia-credenciales.txt` (solo en CloudShell, nunca en el repositorio).

Avance:

```bash
ssh -i ~/.ssh/sentinel-ia.pem ec2-user@<ip> 'tail -n 20 /var/log/sentinel-ia.log; cat /var/log/sentinel-ia-ready'
```

### 5. Verificar

```bash
API=https://api.<ip-con-guiones>.sslip.io
KEY=<api key>
curl -s $API/health/ready
curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/v1/score -d '{}'     # 401 sin clave
```

Swagger en `$API/docs` (botón *Authorize*) y la consola en
`https://dashboard.<ip-con-guiones>.sslip.io`.

### 6. Prueba de carga desde CloudShell

```bash
curl -fsSL https://github.com/grafana/k6/releases/download/v0.55.0/k6-v0.55.0-linux-amd64.tar.gz | tar -xz
mkdir -p reports
API_URL=$API SENTINEL_API_KEY=$KEY DURACION=2m ./k6-v0.55.0-linux-amd64/k6 run load/k6_pico.js
```

Después, el mismo sistema con el modelo en SageMaker:

```bash
ssh -i ~/.ssh/sentinel-ia.pem ec2-user@<ip> "cd /opt/sentinel-ia && \
  sed -i 's/^SCORER_BACKEND=.*/SCORER_BACKEND=sagemaker/; s/^SAGEMAKER_ENDPOINT=.*/SAGEMAKER_ENDPOINT=<endpoint>/' .env && \
  docker compose -f docker-compose.yml -f deploy/docker-compose.aws.yml up -d api"
curl -s $API/health/ready        # "backend": "sagemaker"
```

y se repite k6. El endpoint serverless tiene arranque en frío: conviene enviar
unas 20 transacciones antes de medir y anotar el arranque como hallazgo.

### 7. Evidencias

| Evidencia | Dónde |
|---|---|
| Endpoint `InService` y su configuración | SageMaker → Inference → Endpoints |
| Invocaciones y latencia del endpoint | CloudWatch → Métricas → SageMaker: `Invocations`, `ModelLatency`, `OverheadLatency` |
| Logs de cada servicio | CloudWatch → Grupos de registros → `/sentinel-ia/*` |
| CPU de la instancia durante la carga | EC2 → la instancia → Monitoreo |
| Swagger y dashboard públicos | Las URLs del paso 5 |

### 8. Limpieza

Learner Lab detiene la instancia al cerrar la sesión y la vuelve a encender al
iniciarla; con la IP elástica, las URLs no cambian. Al terminar la evaluación:

```bash
python3 src/training/sagemaker_deploy.py --region us-east-1 --delete <endpoint>
aws ec2 terminate-instances --instance-ids <instancia>
aws ec2 release-address --allocation-id <eipalloc-...>    # después de terminar la instancia
```
